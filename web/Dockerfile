FROM nginx:alpine
COPY nginx.conf /etc/nginx/conf.d/default.conf
COPY index.html style.css helpers.js test_helpers.html /usr/share/nginx/html/
COPY js/ /usr/share/nginx/html/js/
COPY --chmod=755 10-render-app-js.sh /docker-entrypoint.d/
