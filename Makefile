.PHONY: build up down logs clean install deploy

# Docker Compose commands
build:
	docker compose build

up:
	docker compose up -d

down:
	docker compose down

logs:
	docker compose logs -f

clean:
	docker compose down -v

# Kubernetes/Helm commands
install-deps:
	poetry install

build-images:
	docker build -t rainscribe/shared-volume-init:latest -f shared-volume-init/Dockerfile .
	docker build -t rainscribe/audio-extractor:latest -f audio-extractor/Dockerfile .
	docker build -t rainscribe/transcription-service:latest -f transcription-service/Dockerfile .
	docker build -t rainscribe/caption-generator:latest -f caption-generator/Dockerfile .
	docker build -t rainscribe/stream-mirroring:latest -f stream-mirroring/Dockerfile .
	docker build -t rainscribe/nginx:latest -f nginx/Dockerfile .

push-images:
	docker push rainscribe/shared-volume-init:latest
	docker push rainscribe/audio-extractor:latest
	docker push rainscribe/transcription-service:latest
	docker push rainscribe/caption-generator:latest
	docker push rainscribe/stream-mirroring:latest
	docker push rainscribe/nginx:latest

helm-lint:
	helm lint helm-chart/

helm-install:
	helm upgrade --install rainscribe ./helm-chart --namespace rainscribe --create-namespace

helm-uninstall:
	helm uninstall rainscribe --namespace rainscribe

help:
	@echo "Available commands:"
	@echo "  build           - Build all Docker images"
	@echo "  up              - Start all services with Docker Compose"
	@echo "  down            - Stop all services"
	@echo "  logs            - View logs from all services"
	@echo "  clean           - Stop all services and remove volumes"
	@echo "  install-deps    - Install Poetry dependencies"
	@echo "  build-images    - Build Docker images for Kubernetes"
	@echo "  push-images     - Push Docker images to a registry"
	@echo "  helm-lint       - Lint the Helm chart"
	@echo "  helm-install    - Install or upgrade the Helm chart"
	@echo "  helm-uninstall  - Uninstall the Helm chart" 