.PHONY: lint test utest itest

lint:
	uv run ruff format .
	uv run ruff check --fix .

test: utest itest

utest:
	uv run pytest -v

itest:
	docker compose up --build --exit-code-from integration-test
