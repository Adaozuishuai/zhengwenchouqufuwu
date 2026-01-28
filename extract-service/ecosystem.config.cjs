// /opt/extract-service/ecosystem.config.cjs
module.exports = {
  apps: [
    {
      name: "extract-api",
      cwd: "/opt/extract-service",
      script: "./venv/bin/python",
      args: "-m uvicorn app.main:app --host 0.0.0.0 --port 8002",
      autorestart: true,
      max_memory_restart: "300M",
      env: {
        EXTRACT_API_KEY: process.env.EXTRACT_API_KEY || "",
        EXTRACT_MAX_REDIRECTS: process.env.EXTRACT_MAX_REDIRECTS || "5",
        EXTRACT_MAX_HTML_BYTES: process.env.EXTRACT_MAX_HTML_BYTES || "5000000"
      }
    }
  ]
}
