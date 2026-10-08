// Para `npm start` fuera de Docker: reenvía /api/* a la API de FastAPI
// (por default la del compose local, publicada en el puerto 8010).
const backendHost = process.env.BACKEND_HOST || 'localhost';
const backendPort = process.env.BACKEND_PORT || '8010';
const target = `http://${backendHost}:${backendPort}`;

module.exports = {
  '/api': {
    target,
    pathRewrite: { '^/api': '' },
    changeOrigin: true,
  },
};
