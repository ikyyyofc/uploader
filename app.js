const express = require("express");
const multer = require("multer");
const path = require("path");
const fs = require("fs");
const secure = require("ssl-express-www");

const app = express();
const port = 7860;

// Storage engine
const storage = multer.diskStorage({
  destination: "./uploads/",
  filename: function (req, file, cb) {
    const uploadDir = path.join(__dirname, "uploads");

    function generateName() {
      const randomStr = Math.random().toString(36).substring(2, 6); // 4 char random
      const timeStr = Date.now().toString(36); // timestamp pendek
      return timeStr + "-" + randomStr + path.extname(file.originalname);
    }

    let filename = generateName();

    // cek biar ga nabrak
    while (fs.existsSync(path.join(uploadDir, filename))) {
      filename = generateName();
    }

    cb(null, filename);
  }
});

// Initialize upload
const upload = multer({ storage: storage }).single("file");

// secure connection
app.use(secure);

// view engine
app.set("view engine", "ejs");

// static folder
app.use(express.static("./public"));

// form upload
app.get("/", (req, res) => res.render("index", { msg: null, file: null }));

// upload route
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
        const fileUrl = `${req.protocol}://${req.get("host")}/uploads/${req.file.filename}`;

        // hapus file yg lebih dari 7 hari
        const uploadDir = path.join(__dirname, "uploads");
        const maxAge = 7 * 24 * 60 * 60 * 1000; // 7 hari

        fs.readdir(uploadDir, (err, files) => {
          if (err) throw err;

          files.forEach(file => {
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

// akses uploads
app.use("/uploads", express.static("uploads"));

app.listen(port, () => console.log(`Server jalan di port ${port}`));