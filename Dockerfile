FROM python:3.13-alpine
LABEL maintainer="Gilles Reichert"
WORKDIR /
COPY start.sh requirements.txt taposc.py tapo-rest ./
RUN pip3 install -r requirements.txt
EXPOSE 5000
EXPOSE 80

# Make the startup script executable
RUN chmod +x ./start.sh

# Use the startup script as the command
CMD ["./start.sh"]
# Build the Docker image with the command:
# docker build -t taposc . 
# Run the Docker container with the command:
# docker run -d -p 5000:5000 taposc 
# To stop the container, use:
# docker stop <container_id>
# To remove the container, use:
# docker rm <container_id>
# To remove the image, use:
# docker rmi taposc
# To view logs, use:
# docker logs <container_id>
# To run the container in interactive mode, use:
# docker run -it -p 5000:5000 taposc /bin/sh
# To run the container with a specific name, use:
# docker run -d --name my_taposc -p 5000:5000 taposc
# To run the container with a volume, use:
# docker run -d -p 5000:5000 -v /path/to/local/dir:/app taposc
