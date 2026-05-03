.PHONY: production stop logs

production:
	docker compose -f server/docker-compose.yml up --build -d

stop:
	docker compose -f server/docker-compose.yml down

logs:
	docker compose -f server/docker-compose.yml logs -f
