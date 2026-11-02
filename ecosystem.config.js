// PM2 process file for Modmail (JSON database edition).
//   Start:   pm2 start ecosystem.config.js
//   Logs:    pm2 logs modmail
//   Restart: pm2 restart modmail
// Settings come from the .env file in this folder.
const path = require("path");
const isWin = process.platform === "win32";

module.exports = {
  apps: [
    {
      name: "modmail",
      script: "bot.py",
      cwd: __dirname,
      interpreter: path.join(
        __dirname,
        ".venv",
        isWin ? "Scripts" : "bin",
        isWin ? "python.exe" : "python"
      ),
      // ?restart and ?update make the bot exit; PM2 brings it straight back.
      autorestart: true,
      restart_delay: 5000,
      max_restarts: 50,
      min_uptime: "20s",
      // Give it time to finish writing JSON files on shutdown.
      kill_timeout: 10000,
      watch: false,
      env: {
        PYTHONUNBUFFERED: "1",
      },
      time: true,
    },
  ],
};
