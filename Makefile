.PHONY: help lint test
.DEFAULT_GOAL := help

help:
	@echo "Available commands:"
	@echo "  lint - run ruff and mypy"
	@echo "  test - run tests with coverage"

lint:
	ruff check .
	mypy src/scriptdb

test:
	pytest --cov=scriptdb --cov-report=term-missing
