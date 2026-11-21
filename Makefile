.PHONY: production stop logs update-server-deps update-client-deps update-all-deps

# pur rewrites pinned versions in requirements files to the latest releases.
# Override with e.g. `make update-all-deps PUR=pur` if pur is installed directly.
PUR ?= uv tool run pur

production:
	docker compose -f server/docker-compose.yml build --pull
	docker compose -f server/docker-compose.yml up -d

stop:
	docker compose -f server/docker-compose.yml down

logs:
	docker compose -f server/docker-compose.yml logs -f

update-server-deps:
	$(PUR) -r server/requirements.txt

update-client-deps:
	$(PUR) -r desktop/requirements.txt
	$(PUR) -r desktop/requirements-dev.txt

update-all-deps: update-server-deps update-client-deps
