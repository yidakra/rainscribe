FROM nginx:1.23-alpine

# Copy NGINX configuration
COPY nginx/nginx.conf /etc/nginx/nginx.conf
# Remove default conf to avoid conflicts
RUN rm -f /etc/nginx/conf.d/default.conf

# Copy HTML files
COPY nginx/html/ /usr/share/nginx/html/

# Create symbolic links to the shared volume for HLS and WebVTT content
RUN mkdir -p /usr/share/nginx/html && \
    ln -sf /shared-data/hls /usr/share/nginx/html/hls && \
    ln -sf /shared-data/webvtt /usr/share/nginx/html/webvtt

# Expose port
EXPOSE 80

# Start NGINX in foreground
CMD ["nginx", "-g", "daemon off;"] 