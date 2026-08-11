FROM python:3.13-slim-bookworm
LABEL maintainer="Gilles Reichert"

# The application lives at the filesystem root and reads its configuration from
# /app/config.json, which is expected to be a mounted volume.
WORKDIR /
COPY requirements.txt ./
RUN pip3 install --no-cache-dir -r requirements.txt

COPY start.sh taposc.py tapo_config.py tapo_devices.py tapo_power.py \
     tapo_api.py tapo_state.py ./
RUN chmod +x ./start.sh

EXPOSE 5000

CMD ["./start.sh"]

# Build the Docker image with the command:
# docker build -t taposc .
# Run it, mounting the directory that holds config.json:
# docker run -d -p 5000:5000 -v /path/to/local/app:/app --name tapo taposc
# To stop the container, use:
# docker stop <container_id>
# To remove the container, use:
# docker rm <container_id>
# To remove the image, use:
# docker rmi taposc
# To view logs, use:
# docker logs <container_id>
# To run the container in interactive mode, use:
# docker run -it -p 5000:5000 -v /path/to/local/app:/app taposc /bin/sh
