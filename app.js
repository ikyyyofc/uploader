const express = require("express");
const multer = require("multer");
const path = require("path");
const fs = require("fs");
const { spawn } = require("child_process");
const secure = require("ssl-express-www");
const { compress: headroomCompress, HeadroomClient } = require("headroom-ai");

const app = express();
const port = process.env.PORT || 7860;
const HEADROOM_PORT = process.env.HEADROOM_PORT || 8787;
const HEADROOM_BASE_URL = process.env.HEADROOM_BASE_URL || `http://127.0.0.1:${HEADROOM_PORT}`;

const headroomClient = new HeadroomClient({
  baseUrl: HEADROOM_BASE_URL,
  fallback: false,
  timeout: 30000
});

// Background Headroom Python service manager
let headroomProc = null;
let headroomStartingPromise = null;
let headroomReadyCache = false;

async function isHeadroomHealthy() {
  try {
    const res = await fetch(`${HEADROOM_BASE_URL}/health`, {
      signal: AbortSignal.timeout(1200)
    });
    headroomReadyCache = res.ok;
    return res.ok;
  } catch (_) {
    headroomReadyCache = false;
    return false;
  }
}

function resolveHeadroomCommand() {
  const scriptPath = path.join(__dirname, "headroom_service.py");
  if (fs.existsSync("/opt/headroom-venv/bin/python")) {
    return { cmd: "/opt/headroom-venv/bin/python", args: [scriptPath] };
  }
  if (fs.existsSync("/data/data/com.termux/files/usr/bin/proot-distro")) {
    return {
      cmd: "/data/data/com.termux/files/usr/bin/proot-distro",
      args: ["login", "debian", "--", "/opt/headroom-venv/bin/python", scriptPath]
    };
  }
  return { cmd: "python3", args: [scriptPath] };
}

async function ensureHeadroomService() {
  if (headroomReadyCache && headroomProc && !headroomProc.killed && headroomProc.exitCode === null) {
    return true;
  }
  if (await isHeadroomHealthy()) {
    return true;
  }
  if (headroomStartingPromise) {
    return headroomStartingPromise;
  }

  headroomStartingPromise = (async () => {
    try {
      if (!headroomProc || headroomProc.killed || headroomProc.exitCode !== null) {
        const { cmd, args } = resolveHeadroomCommand();
        console.log(`[Headroom] Starting service: ${cmd} ${args.join(" ")}`);
        headroomProc = spawn(cmd, args, {
          env: {
            ...process.env,
            HEADROOM_PORT: String(HEADROOM_PORT),
            HEADROOM_OFFLINE: "1",
            HEADROOM_TIKTOKEN_LOAD_TIMEOUT_SECONDS: "0.001",
            HEADROOM_DETECT_BACKEND: "python"
          },
          stdio: ["ignore", "pipe", "pipe"]
        });

        headroomProc.stdout.on("data", data => {
          process.stdout.write(`[Headroom] ${data}`);
        });
        headroomProc.stderr.on("data", data => {
          process.stderr.write(`[Headroom] ${data}`);
        });
        headroomProc.on("exit", code => {
          console.log(`[Headroom] Service exited with code ${code}`);
          headroomProc = null;
          headroomReadyCache = false;
        });
      }

      const deadline = Date.now() + 25000;
      while (Date.now() < deadline) {
        if (await isHeadroomHealthy()) {
          return true;
        }
        await new Promise(r => setTimeout(r, 300));
      }
      return false;
    } finally {
      headroomStartingPromise = null;
    }
  })();

  return headroomStartingPromise;
}

// Storage engine for disk uploads (/upload)
const storage = multer.diskStorage({
  destination: "./uploads/",
  filename: function (req, file, cb) {
    const uploadDir = path.join(__dirname, "uploads");

    function generateName() {
      const randomStr = Math.random().toString(36).substring(2, 6);
      const timeStr = Date.now().toString(36);
      return timeStr + "-" + randomStr + path.extname(file.originalname);
    }

    let filename = generateName();

    while (fs.existsSync(path.join(uploadDir, filename))) {
      filename = generateName();
    }

    cb(null, filename);
  }
});

// Initialize disk upload & memory upload (for /compress file upload)
const upload = multer({ storage: storage }).single("file");
const memoryUpload = multer({
  storage: multer.memoryStorage(),
  limits: { fileSize: 25 * 1024 * 1024 }
}).single("file");

// Trust reverse proxy headers (Koyeb, Vercel, Cloudflare, etc.)
app.set("trust proxy", true);

// CORS support for external gateways like techwiz37/routermod (9Router)
app.use((req, res, next) => {
  res.setHeader("Access-Control-Allow-Origin", "*");
  res.setHeader("Access-Control-Allow-Methods", "GET, POST, PUT, PATCH, DELETE, OPTIONS");
  res.setHeader("Access-Control-Allow-Headers", "Content-Type, Authorization, X-Headroom-Proxy-Token, X-Headroom-Mode, X-Headroom-Session-Id, X-Headroom-Bypass");
  if (req.method === "OPTIONS") {
    return res.status(204).end();
  }
  next();
});

// Body parsers for JSON & URL-encoded payloads
app.use(express.json({ limit: "50mb" }));
app.use(express.urlencoded({ extended: true, limit: "50mb" }));

// Fast (<5ms) health check endpoint for Koyeb, Docker HEALTHCHECK, and routermod probeProxyRunning (1500ms timeout)
function handleHealthCheck(req, res) {
  if (!headroomReadyCache) {
    ensureHeadroomService().catch(() => {});
  }
  res.status(200).json({
    service: "headroom-proxy",
    status: "healthy",
    ready: true,
    headroom_ready: headroomReadyCache,
    version: "0.44.0"
  });
}

app.get("/health", handleHealthCheck);
app.get("/compress/health", handleHealthCheck);

// Secure connection (only for browser GET requests to main uploader pages; never redirect API/Headroom requests)
app.use((req, res, next) => {
  const host = req.hostname || "";
  const forwardedProto = (req.headers["x-forwarded-proto"] || "").toLowerCase();
  if (
    req.method !== "GET" ||
    !forwardedProto ||
    host === "localhost" ||
    host === "127.0.0.1" ||
    host === "::1" ||
    req.path === "/health" ||
    req.path.startsWith("/compress") ||
    req.path.startsWith("/v1/") ||
    req.path.startsWith("/dashboard") ||
    req.path.startsWith("/stats") ||
    req.path.startsWith("/transformations")
  ) {
    return next();
  }
  return secure(req, res, next);
});

// View engine
app.set("view engine", "ejs");

// Static folder
app.use(express.static("./public"));

// Helper to list uploaded files
function getUploadedFileList() {
  const uploadDir = path.join(__dirname, "uploads");
  if (!fs.existsSync(uploadDir)) return [];
  const files = fs.readdirSync(uploadDir);
  return files
    .filter(file => file !== ".gitkeep")
    .map(file => {
      const filePath = path.join(uploadDir, file);
      const stats = fs.statSync(filePath);
      return {
        name: file,
        size: (stats.size / 1024).toFixed(2) + " KB",
        atime: stats.atime,
        mtime: stats.mtime,
        url: `/uploads/${file}`
      };
    })
    .sort((a, b) => b.mtime - a.mtime);
}

// Form upload
app.get("/", (req, res) => res.render("index", { msg: null, file: null }));

// List files
app.get("/files", (req, res) => {
  try {
    const fileList = getUploadedFileList();
    res.render("files", { files: fileList });
  } catch (err) {
    return res.status(500).render("index", { msg: "Error: Gagal membaca direktori upload", file: null });
  }
});

// Upload route
app.post("/upload", (req, res) => {
  upload(req, res, err => {
    if (err) {
      return res.status(500).json({ error: err.message });
    } else {
      if (req.file == undefined) {
        return res
          .status(400)
          .json({ error: "Error: Tidak Ada File yang Dipilih!" });
      } else {
        const fileUrl = `https://${req.get("host")}/uploads/${req.file.filename}`;

        // Hapus file yg lebih dari 7 hari
        const uploadDir = path.join(__dirname, "uploads");
        const maxAge = 7 * 24 * 60 * 60 * 1000;

        fs.readdir(uploadDir, (err, files) => {
          if (err) throw err;

          files.forEach(file => {
            if (file === ".gitkeep") return;
            const filePath = path.join(uploadDir, file);
            const stats = fs.statSync(filePath);
            const now = Date.now();

            if (now - stats.mtimeMs > maxAge) {
              fs.unlinkSync(filePath);
            }
          });

          return res.status(200).json({
            message: "File Diunggah!",
            fileUrl: fileUrl
          });
        });
      }
    }
  });
});

// Akses uploads dengan paksa download
app.get("/uploads/:filename", (req, res) => {
  const filename = path.basename(req.params.filename);
  const filePath = path.join(__dirname, "uploads", filename);

  if (fs.existsSync(filePath)) {
    res.download(filePath);
  } else {
    res.status(404).send("File tidak ditemukan");
  }
});

// ============================================================================
// Headroom AI Compression Endpoints (/compress, /v1/compress, & routermod proxy)
// ============================================================================

app.get("/compress", async (req, res) => {
  const wantsJson =
    req.query.format === "json" ||
    (req.headers.accept &&
      req.headers.accept.includes("application/json") &&
      !req.headers.accept.includes("text/html"));

  if (wantsJson) {
    ensureHeadroomService().catch(() => {});
    return res.json({
      endpoint: "/compress",
      package: "headroom-ai",
      version: "0.44.0",
      repository: "https://github.com/headroomlabs-ai/headroom",
      routermod_compatible: true,
      routermod_urls: [
        `https://${req.get("host")}/compress`,
        `https://${req.get("host")}`
      ],
      health: {
        service: "headroom-proxy",
        status: "healthy",
        ready: true,
        version: "0.44.0"
      },
      usage: {
        ui: "GET /compress (browser)",
        routermod_compress: "POST /compress/v1/compress or POST /v1/compress",
        compress_text: "POST /compress { text: '...', model: 'gpt-4o', strategy: 'auto' }",
        compress_messages: "POST /compress { messages: [{ role: 'user', content: '...' }], model: 'gpt-4o' }",
        compress_file: "POST /compress (multipart/form-data with field 'file' or JSON { filename: '...' })",
        retrieve_ccr: "POST /compress/retrieve { hash: '...' }",
        dashboard: "GET /dashboard or GET /compress/dashboard",
        stats: "GET /stats or GET /compress/stats",
        health: "GET /health or GET /compress/health"
      }
    });
  }

  const fileList = getUploadedFileList();
  res.render("compress", {
    files: fileList,
    headroomVersion: "0.44.0"
  });
});

// Proxy helper for Headroom stats/dashboard endpoints used by routermod
async function proxyToHeadroomGet(targetPath, req, res) {
  try {
    await ensureHeadroomService();
    const qs = req.url.includes("?") ? req.url.slice(req.url.indexOf("?")) : "";
    const resp = await fetch(`${HEADROOM_BASE_URL}${targetPath}${qs}`, {
      signal: AbortSignal.timeout(10000)
    });
    const contentType = resp.headers.get("content-type") || "application/json";
    res.status(resp.status).setHeader("Content-Type", contentType);
    const text = await resp.text();
    res.send(text);
  } catch (err) {
    res.status(503).json({ error: err.message });
  }
}

app.get(["/stats", "/compress/stats"], (req, res) => proxyToHeadroomGet("/stats", req, res));
app.get(["/stats-history", "/compress/stats-history"], (req, res) => proxyToHeadroomGet("/stats-history", req, res));
app.get(["/stats-lifetime", "/compress/stats-lifetime"], (req, res) => proxyToHeadroomGet("/stats-lifetime", req, res));
app.get(["/transformations/feed", "/compress/transformations/feed"], (req, res) => proxyToHeadroomGet("/transformations/feed", req, res));
app.get(["/dashboard", "/compress/dashboard"], (req, res) => proxyToHeadroomGet("/dashboard", req, res));

async function handleRetrieveCCR(req, res) {
  try {
    await ensureHeadroomService();
    const hash = req.body?.hash || req.body?.key || req.query?.hash;
    if (!hash) {
      return res.status(400).json({ error: "Parameter 'hash' wajib diisi" });
    }
    const resp = await headroomClient.rawFetch("/v1/retrieve", {
      method: "POST",
      body: { hash }
    });
    const data = await resp.json();
    res.json(data);
  } catch (err) {
    res.status(err.statusCode || 404).json({ error: err.message });
  }
}

app.post(["/compress/retrieve", "/v1/retrieve", "/compress/v1/retrieve"], handleRetrieveCCR);

async function handleCompressRequest(req, res) {
  const t0 = Date.now();
  try {
    const ready = await ensureHeadroomService();
    if (!ready) {
      return res.status(503).json({
        error: "Headroom compression engine belum siap (service di port 8787 tidak merespons)."
      });
    }

    const body = req.body || {};
    const model = body.model || req.query.model || "gpt-4o";
    const strategy = body.strategy || body.config?.strategy || req.query.strategy || "auto";
    const targetRatioRaw = body.target_ratio ?? body.targetRatio ?? body.config?.target_ratio;
    const targetRatio =
      targetRatioRaw !== undefined && targetRatioRaw !== "" ? Number(targetRatioRaw) : undefined;
    const tokenBudget = body.token_budget || body.tokenBudget ? Number(body.token_budget || body.tokenBudget) : undefined;

    let messages = null;
    let isSingleTextMode = false;
    let rawInputText = null;
    let sourceLabel = "inline";

    // Case 1: Multipart uploaded file
    if (req.file && req.file.buffer) {
      rawInputText = req.file.buffer.toString("utf-8");
      isSingleTextMode = true;
      sourceLabel = `upload:${req.file.originalname}`;
    }
    // Case 2: Existing file from ./uploads
    else if (body.filename) {
      const safeName = path.basename(String(body.filename));
      const targetPath = path.join(__dirname, "uploads", safeName);
      if (!fs.existsSync(targetPath)) {
        return res.status(404).json({ error: `File '${safeName}' tidak ditemukan di direktori uploads.` });
      }
      rawInputText = fs.readFileSync(targetPath, "utf-8");
      isSingleTextMode = true;
      sourceLabel = `uploads/${safeName}`;
    }
    // Case 3: LLM messages array (OpenAI / Anthropic / routermod / Headroom SDK)
    else if (Array.isArray(body.messages)) {
      messages = body.messages;
      sourceLabel = "messages";
    }
    // Case 4: Responses input array
    else if (Array.isArray(body.input)) {
      messages = body.input;
      sourceLabel = "input";
    }
    // Case 5: Raw text / content / input string
    else if (typeof (body.text ?? body.content ?? body.input) === "string") {
      rawInputText = body.text ?? body.content ?? body.input;
      isSingleTextMode = true;
      sourceLabel = "text";
    } else {
      return res.status(400).json({
        error: "Masukkan field 'text', 'messages' (array), 'filename', atau unggah 'file' (multipart/form-data)."
      });
    }

    if (isSingleTextMode) {
      messages = [{ role: "user", content: rawInputText }];
    }

    const charCountBefore = isSingleTextMode
      ? rawInputText.length
      : messages.reduce((acc, m) => acc + (typeof m?.content === "string" ? m.content.length : 0), 0);

    const compressConfig = {
      ...(typeof body.config === "object" && body.config ? body.config : {}),
      strategy,
      compress_user_messages: body.config?.compress_user_messages ?? true,
      protect_recent: body.config?.protect_recent ?? 0,
      protect_analysis_context: body.config?.protect_analysis_context ?? false,
      min_tokens_to_compress: body.config?.min_tokens_to_compress ?? 10
    };
    if (targetRatio !== undefined && !Number.isNaN(targetRatio)) {
      compressConfig.target_ratio = targetRatio;
    }

    // For array messages (e.g. from routermod), call headroomClient.compressRaw directly
    // so every message field (role, tool_call_id, tool_calls, name) is preserved 1:1 without format-conversion drift.
    let rawData;
    if (!isSingleTextMode) {
      try {
        rawData = await headroomClient.compressRaw({
          messages,
          model,
          ...(tokenBudget ? { token_budget: tokenBudget } : {}),
          config: compressConfig
        });
      } catch (e) {
        headroomReadyCache = false;
        await ensureHeadroomService();
        rawData = await headroomClient.compressRaw({
          messages,
          model,
          ...(tokenBudget ? { token_budget: tokenBudget } : {}),
          config: compressConfig
        });
      }
    } else {
      const sdkResult = await headroomCompress(messages, {
        client: headroomClient,
        model,
        tokenBudget,
        config: compressConfig
      });
      rawData = {
        messages: sdkResult.messages,
        tokens_before: sdkResult.tokensBefore,
        tokens_after: sdkResult.tokensAfter,
        tokens_saved: sdkResult.tokensSaved,
        compression_ratio: sdkResult.compressionRatio,
        transforms_applied: sdkResult.transformsApplied,
        ccr_hashes: sdkResult.ccrHashes
      };
    }

    const compressedMessages = rawData.messages || messages;
    const compressedText =
      isSingleTextMode && compressedMessages[0] && typeof compressedMessages[0].content === "string"
        ? compressedMessages[0].content
        : undefined;

    const charCountAfter =
      compressedText !== undefined
        ? compressedText.length
        : compressedMessages.reduce((acc, m) => acc + (typeof m?.content === "string" ? m.content.length : 0), 0);

    const tokensBefore = rawData.tokens_before ?? 0;
    const tokensAfter = rawData.tokens_after ?? 0;
    const tokensSaved = rawData.tokens_saved ?? Math.max(0, tokensBefore - tokensAfter);
    const compressionRatio = rawData.compression_ratio ?? (tokensBefore > 0 ? Number((tokensAfter / tokensBefore).toFixed(4)) : 1.0);
    const savingsPercentage =
      tokensBefore > 0 ? Number(((tokensSaved / tokensBefore) * 100).toFixed(2)) : 0.0;
    const transformsApplied = rawData.transforms_applied || [];
    const ccrHashes = rawData.ccr_hashes || [];

    return res.status(200).json({
      status: "success",
      source: sourceLabel,
      model,
      strategy,
      ...(compressedText !== undefined ? { compressed_text: compressedText, compressedText } : {}),
      messages: compressedMessages,
      tokens_before: tokensBefore,
      tokens_after: tokensAfter,
      tokens_saved: tokensSaved,
      compression_ratio: compressionRatio,
      savings_percentage: savingsPercentage,
      transforms_applied: transformsApplied,
      ccr_hashes: ccrHashes,
      tokensBefore,
      tokensAfter,
      tokensSaved,
      compressionRatio,
      savingsPercentage,
      transformsApplied,
      ccrHashes,
      char_count_before: charCountBefore,
      char_count_after: charCountAfter,
      latency_ms: Date.now() - t0
    });
  } catch (err) {
    console.error("[Headroom /compress error]:", err);
    return res.status(err.statusCode || 500).json({
      error: err.message || "Terjadi kesalahan saat mengompres data dengan Headroom."
    });
  }
}

function handleCompressRoute(req, res) {
  const contentType = req.headers["content-type"] || "";
  if (contentType.includes("multipart/form-data")) {
    return memoryUpload(req, res, err => {
      if (err) {
        return res.status(400).json({ error: err.message });
      }
      return handleCompressRequest(req, res);
    });
  }
  return handleCompressRequest(req, res);
}

// Mount compression on all 3 paths so routermod works whether Proxy URL is set to:
// - https://<domain>/compress  (routermod calls POST /compress/v1/compress)
// - https://<domain>           (routermod calls POST /v1/compress)
// - direct POST /compress
app.post(["/compress", "/v1/compress", "/compress/v1/compress"], handleCompressRoute);

// Start server on 0.0.0.0 (required by Koyeb) and warm up Headroom service in background
if (require.main === module) {
  app.listen(port, "0.0.0.0", () => {
    console.log(`Server jalan di 0.0.0.0:${port} (Endpoint Headroom: http://localhost:${port}/compress)`);
    ensureHeadroomService().then(ok => {
      if (ok) {
        console.log(`[Headroom] Compression engine aktif di ${HEADROOM_BASE_URL}`);
      } else {
        console.warn(`[Headroom] Peringatan: Gagal memulai service Headroom di ${HEADROOM_BASE_URL}`);
      }
    });
  });
}

module.exports = app;
