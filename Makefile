.PHONY: bootstrap up down

bootstrap:
	./scripts/bootstrap.sh

up:
	./scripts/bootstrap.sh --up

down:
	docker compose down
