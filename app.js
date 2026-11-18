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

async function isHeadroomHealthy() {
  try {
    const res = await fetch(`${HEADROOM_BASE_URL}/health`, {
      signal: AbortSignal.timeout(1500)
    });
    return res.ok;
  } catch (_) {
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
        });
      }

      const deadline = Date.now() + 20000;
      while (Date.now() < deadline) {
        if (await isHeadroomHealthy()) {
          return true;
        }
        await new Promise(r => setTimeout(r, 350));
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

// Body parsers for JSON & URL-encoded payloads
app.use(express.json({ limit: "50mb" }));
app.use(express.urlencoded({ extended: true, limit: "50mb" }));

// Health check endpoint for Koyeb & Docker HEALTHCHECK
app.get("/health", async (req, res) => {
  const headroomReady = await isHeadroomHealthy();
  res.status(200).json({
    status: "ok",
    headroom_ready: headroomReady,
    headroom_url: HEADROOM_BASE_URL
  });
});

// Secure connection (skip internal Koyeb health checks, localhost/127.0.0.1, and API endpoints)
app.use((req, res, next) => {
  const host = req.hostname || "";
  const forwardedProto = (req.headers["x-forwarded-proto"] || "").toLowerCase();
  if (
    !forwardedProto ||
    host === "localhost" ||
    host === "127.0.0.1" ||
    host === "::1" ||
    req.path === "/health" ||
    req.path.startsWith("/compress") ||
    req.path.startsWith("/v1/")
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
// Headroom AI Compression Endpoints (/compress & /v1/compress)
// ============================================================================

app.get("/compress", async (req, res) => {
  const wantsJson =
    req.query.format === "json" ||
    (req.headers.accept &&
      req.headers.accept.includes("application/json") &&
      !req.headers.accept.includes("text/html"));

  if (wantsJson) {
    await ensureHeadroomService();
    let health = { status: "starting" };
    try {
      health = await headroomClient.health();
    } catch (_) {}
    return res.json({
      endpoint: "/compress",
      package: "headroom-ai",
      repository: "https://github.com/headroomlabs-ai/headroom",
      health,
      usage: {
        ui: "GET /compress (browser)",
        compress_text: "POST /compress { text: '...', model: 'gpt-4o', strategy: 'auto' }",
        compress_messages: "POST /compress { messages: [{ role: 'user', content: '...' }], model: 'gpt-4o' }",
        compress_file: "POST /compress (multipart/form-data with field 'file' or JSON { filename: '...' })",
        retrieve_ccr: "POST /compress/retrieve { hash: '...' }",
        stats: "GET /compress/stats",
        health: "GET /compress/health"
      }
    });
  }

  const fileList = getUploadedFileList();
  res.render("compress", {
    files: fileList,
    headroomVersion: "0.44.0"
  });
});

app.get("/compress/health", async (req, res) => {
  try {
    await ensureHeadroomService();
    const health = await headroomClient.health();
    res.json(health);
  } catch (err) {
    res.status(503).json({ status: "unhealthy", error: err.message });
  }
});

app.get("/compress/stats", async (req, res) => {
  try {
    await ensureHeadroomService();
    const stats = await headroomClient.proxyStats();
    res.json(stats);
  } catch (err) {
    res.status(503).json({ error: err.message });
  }
});

app.post("/compress/retrieve", async (req, res) => {
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
});

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
    // Case 3: LLM messages array
    else if (Array.isArray(body.messages)) {
      messages = body.messages;
      sourceLabel = "messages";
    }
    // Case 4: Raw text / content / input string
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
      : messages.reduce((acc, m) => acc + (typeof m.content === "string" ? m.content.length : JSON.stringify(m.content || "").length), 0);

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

    // Call Headroom via official headroom-ai npm SDK
    const result = await headroomCompress(messages, {
      client: headroomClient,
      model,
      tokenBudget,
      config: compressConfig
    });

    const compressedMessages = result.messages || messages;
    const compressedText =
      isSingleTextMode && compressedMessages[0] && typeof compressedMessages[0].content === "string"
        ? compressedMessages[0].content
        : undefined;

    const charCountAfter =
      compressedText !== undefined
        ? compressedText.length
        : compressedMessages.reduce((acc, m) => acc + (typeof m.content === "string" ? m.content.length : JSON.stringify(m.content || "").length), 0);

    const tokensBefore = result.tokensBefore ?? 0;
    const tokensAfter = result.tokensAfter ?? 0;
    const tokensSaved = result.tokensSaved ?? Math.max(0, tokensBefore - tokensAfter);
    const compressionRatio = result.compressionRatio ?? (tokensBefore > 0 ? Number((tokensAfter / tokensBefore).toFixed(4)) : 1.0);
    const savingsPercentage =
      tokensBefore > 0 ? Number(((tokensSaved / tokensBefore) * 100).toFixed(2)) : 0.0;

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
      transforms_applied: result.transformsApplied || [],
      ccr_hashes: result.ccrHashes || [],
      tokensBefore,
      tokensAfter,
      tokensSaved,
      compressionRatio,
      savingsPercentage,
      transformsApplied: result.transformsApplied || [],
      ccrHashes: result.ccrHashes || [],
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

app.post("/compress", (req, res) => {
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
});

app.post("/v1/compress", (req, res) => handleCompressRequest(req, res));

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
