FROM busybox:1.36

# Copy initialization script
COPY shared-volume-init/init.sh /init.sh

# Make the script executable
RUN chmod +x /init.sh

# Command to run the initialization script
CMD ["/init.sh"] 