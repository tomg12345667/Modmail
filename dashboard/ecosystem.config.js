// PM2 process file for the Modmail web dashboard.
//   pm2 start ecosystem.config.js
const path = require("path");
module.exports = {
  apps: [
    {
      name: "modmail-dashboard",
      script: "app.py",
      cwd: __dirname,
      interpreter: path.join(__dirname, ".venv", "bin", "python"),
      autorestart: true,
      restart_delay: 3000,
      env: { PYTHONUNBUFFERED: "1" },
      time: true,
    },
  ],
};
