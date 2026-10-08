.DEFAULT_GOAL := help

.PHONY: help install doctor version

help: ## Show this help message
	@echo "mike_trade operator commands:"
	@grep -E '^[a-zA-Z_-]+:.*?## .*$$' $(MAKEFILE_LIST) | sort | awk 'BEGIN {FS = ":.*?## "}; {printf "  \033[36m%-15s\033[0m %s\n", $$1, $$2}'

install: ## Install reproducible dependencies incl. dev tooling (uv sync --frozen)
	uv sync --frozen

doctor: ## Run local read-only environment and dependency health checks
	uv run mike-trade doctor

version: ## Display application version
	uv run mike-trade version
