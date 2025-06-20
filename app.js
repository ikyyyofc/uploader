const express = require("express");
const ffmpeg = require('fluent-ffmpeg');
const tmp = require('tmp');
const multer = require("multer");
const path = require("path");
const fs = require("fs");
const secure = require("ssl-express-www");
const fileUpload = require('express-fileupload');

const app = express();
const port = 7860;

// Set storage engine
const storage = multer.diskStorage({
    destination: "./uploads/",
    filename: function (req, file, cb) {
        cb(
            null,
            file.fieldname + "-" + Date.now() + path.extname(file.originalname)
        );
    }
});

// Initialize upload
const upload = multer({
    storage: storage
}).single("file");

// Set Connection secure
app.use(secure);
// Set view engine
app.set("view engine", "ejs");

// Set static folder
app.use(express.static("./public"));
app.use(fileUpload());
// Route untuk menampilkan formulir pengunggahan
app.get("/", (req, res) => res.render("index", { msg: null, file: null }));

// Route untuk menangani pengunggahan file
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
                const fileUrl = `${req.protocol}://${req.get("host")}/uploads/${
                    req.file.filename
                }`;

                // Hapus semua file jika ukuran folder "uploads" mencapai 300 MB
                const uploadDir = path.join(__dirname, "uploads");
                const maxUploadSize = 300 * 1024 * 1024; // 300 MB

                fs.readdir(uploadDir, (err, files) => {
                    if (err) throw err;

                    let totalSize = 0;

                    files.forEach(file => {
                        const filePath = path.join(uploadDir, file);
                        const stats = fs.statSync(filePath);
                        totalSize += stats.size;
                    });

                    if (totalSize > maxUploadSize) {
                        // Hapus semua file dalam folder "uploads"
                        files.forEach(file => {
                            const filePath = path.join(uploadDir, file);
                            fs.unlinkSync(filePath);
                        });
                    }

                    // Kirim respon JSON
                    return res.status(200).json({
                        message: "File Diunggah!",
                        fileUrl: fileUrl
                    });
                });
            }
        }
    });
});
/**
 * Endpoint untuk memotong video
 * @route POST /split-video
 * @param {File} videoFile - File video dalam format MP4
 * @returns {Object} { segments: [Buffer1, Buffer2, ...] }
 */
app.post('/split-video', async (req, res) => {
  if (!req.files || !req.files.videoFile) {
    return res.status(400).json({ error: 'File video tidak ditemukan' });
  }

  const videoBuffer = req.files.videoFile.data;

  try {
    // Buat file sementara untuk input video
    const tmpInput = tmp.fileSync({ postfix: '.mp4' });
    fs.writeFileSync(tmpInput.fd, videoBuffer);

    // 1. Cek durasi video
    const duration = await new Promise((resolve, reject) => {
      ffmpeg.ffprobe(tmpInput.name, (err, metadata) => {
        if (err) reject(err);
        resolve(metadata.format.duration);
      });
    });

    // Hapus file input sementara
    fs.unlinkSync(tmpInput.name); // ⬅️ Hapus file input

    if (duration <= 60) {
      return res.json({
        segments: [videoBuffer.toString('base64')], // Konversi ke Base64 untuk JSON
      });
    }

    const segments = Math.ceil(duration / 60);
    const segmentBuffers = [];

    // 2. Bagi video menjadi bagian 1 menit
    for (let i = 0; i < segments; i++) {
      const startTime = i * 60;
      const tmpOutput = tmp.fileSync({ postfix: `.part${i}.mp4` });

      await new Promise((resolve, reject) => {
        ffmpeg(tmpInput.name)
          .setStartTime(startTime)
          .setDuration(60)
          .videoCodec('copy')   // Pertahankan kualitas
          .audioCodec('copy')   // Pertahankan kualitas
          .output(tmpOutput.name)
          .on('end', resolve)
          .on('error', reject)
          .run();
      });

      // 3. Baca hasil potongan sebagai buffer dan hapus file
      const segmentBuffer = fs.readFileSync(tmpOutput.name);
      fs.unlinkSync(tmpOutput.name); // ⬅️ Hapus file output
      segmentBuffers.push(segmentBuffer.toString('base64'));
    }

    res.json({ segments: segmentBuffers });
  } catch (error) {
    console.error(error);
    res.status(500).json({ error: 'Gagal memproses video' });
  }
});

// Middleware untuk menangani file statis
app.use("/uploads", express.static("uploads"));

app.listen(port, () => console.log(`Server started on port ${port}`));
