const express = require('express');
const ffmpeg = require('fluent-ffmpeg');
const tmp = require('tmp');
const multer = require('multer');
const path = require('path');
const fs = require('fs/promises');
const secure = require('ssl-express-www');

const app = express();
const PORT = 7860;

// Konfigurasi penyimpanan multer
const storage = multer.diskStorage({
  destination: './uploads/',
  filename: (req, file, cb) => {
    const ext = path.extname(file.originalname);
    const filename = `video-${Date.now()}${ext}`;
    cb(null, filename);
  }
});

// Middleware upload
const upload = multer({
  storage,
  limits: { fileSize: 100 * 1024 * 1024 }, // Batas ukuran file 100MB
}).single('videoFile');

// Middleware keamanan
app.use(secure);

app.set("view engine", "ejs");

// Middleware untuk folder statis
app.use(express.static('public'));
app.use('/processed', express.static('processed'));

// Validasi file video
const validateVideoFile = (file) => {
  const allowedTypes = /mp4|mov|avi|mkv/;
  const ext = allowedTypes.test(path.extname(file.originalname).toLowerCase());
  const mime = allowedTypes.test(file.mimetype);
  return ext && mime;
};

// Hapus file tertua jika folder uploads melebihi batas
const cleanupUploads = async () => {
  const uploadDir = path.join(__dirname, 'uploads');
  const maxFiles = 50;
  const files = await fs.readdir(uploadDir);
  if (files.length > maxFiles) {
    // Urutkan berdasarkan waktu modifikasi
    const sorted = await Promise.all(
      files.map(async (file) => {
        const stats = await fs.stat(path.join(uploadDir, file));
        return { file, mtime: stats.mtime };
      })
    );
    sorted.sort((a, b) => a.mtime - b.mtime);
    await fs.unlink(path.join(uploadDir, sorted[0].file));
  }
};

// Route utama
app.get('/', (req, res) => res.render('index', { msg: null, file: null }));

// Route upload file
app.post('/upload', (req, res) => {
  upload(req, res, async (err) => {
    if (err) return res.status(500).json({ error: err.message });
    if (!req.file) return res.status(400).json({ error: 'Tidak ada file diunggah' });
    if (!validateVideoFile(req.file)) return res.status(400).json({ error: 'Hanya file video yang diperbolehkan' });

    await cleanupUploads();
    const fileUrl = `${req.protocol}://${req.get('host')}/uploads/${req.file.filename}`;
    res.json({ message: 'File diunggah!', fileUrl });
  });
});

// Endpoint pemotongan video
app.post('/split-video', (req, res) => {
  upload(req, res, async (err) => {
    if (err) return res.status(500).json({ error: err.message });
    if (!req.file) return res.status(400).json({ error: 'Tidak ada file diunggah' });
    if (!validateVideoFile(req.file)) return res.status(400).json({ error: 'Hanya file video yang diperbolehkan' });

    const inputPath = req.file.path;
    const outputDir = path.join(__dirname, 'processed');
    const segmentUrls = [];

    try {
      // Cek durasi video
      const duration = await new Promise((resolve, reject) => {
        ffmpeg.ffprobe(inputPath, (err, metadata) => {
          if (err) reject(err);
          resolve(metadata.format.duration);
        });
      });

      if (duration <= 60) {
        const url = `${req.protocol}://${req.get('host')}/uploads/${req.file.filename}`;
        return res.json({ segments: [url] });
      }

      const segments = Math.ceil(duration / 60);

      for (let i = 0; i < segments; i++) {
        const startTime = i * 60;
        const outputName = `part-${i}-${Date.now()}.mp4`;
        const outputPath = path.join(outputDir, outputName);

        await new Promise((resolve, reject) => {
          ffmpeg(inputPath)
            .setStartTime(startTime)
            .setDuration(60)
            .videoCodec('copy')
            .audioCodec('copy')
            .output(outputPath)
            .on('end', resolve)
            .on('error', reject)
            .timeout(600) // Timeout setelah 600 detik
            .run();
        });

        segmentUrls.push(`${req.protocol}://${req.get('host')}/processed/${outputName}`);
      }

      // Hapus file input setelah selesai
      await fs.unlink(inputPath);

      res.json({ segments: segmentUrls });
    } catch (error) {
      console.error('Gagal memproses video:', error);
      await fs.unlink(inputPath).catch(() => {});
      res.status(500).json({ error: 'Gagal memproses video' });
    }
  });
});

// Jalankan server
app.listen(PORT, () => {
  console.log(`Server berjalan di https://localhost:${PORT}`); 
});