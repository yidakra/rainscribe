FROM alpine:3.17

WORKDIR /app

# Copy initialization script
COPY shared-volume-init/init.sh /app/init.sh

# Make it executable
RUN chmod +x /app/init.sh

# Run script when container starts
CMD ["/app/init.sh"] 