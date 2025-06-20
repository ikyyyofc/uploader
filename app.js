const express = require("express");
const multer = require("multer");
const path = require("path");
const fs = require("fs");
const secure = require("ssl-express-www");

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

// Middleware untuk menangani file statis
app.use("/uploads", express.static("uploads"));

app.listen(port, () => console.log(`Server started on port ${port}`));
