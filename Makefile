# PCB design agent — developer entry points.
# Everything here runs against the source tree; no packaging step is required.

ROOT       := $(abspath $(dir $(lastword $(MAKEFILE_LIST))))
VENV       ?= $(ROOT)/.venv
PY         := $(VENV)/bin/python
PIP        := $(VENV)/bin/pip
PKG_ROOT   := $(ROOT)/anna-app/executas/pcb-designer
export PYTHONPATH := $(PKG_ROOT)
export PCB_AI_PROJECT ?= $(ROOT)/build/demo-project
export PCB_AI_WORKDIR   ?= $(ROOT)/build
export PCB_AI_LLM_PROVIDER ?= dummy

.PHONY: help bootstrap test test-verbose lint fmt tools backend demo rpc clean kiwad-check

help:
	@printf "PCB design agent targets\n"
	@printf "  bootstrap     create .venv and install deps (no uv / no KiCad required)\n"
	@printf "  test          run the pytest suite (works without KiCad or API keys)\n"
	@printf "  lint / fmt    ruff checks / autofix\n"
	@printf "  tools         list every tool, its safety class and maturity\n"
	@printf "  backend       show which EDA backends are usable here\n"
	@printf "  demo          end-to-end: requirements -> BOM -> footprint -> pipeline\n"
	@printf "  rpc           start the Anna plugin and call full_pipeline over stdio\n"

bootstrap:
	@bash scripts/bootstrap.sh

test:
	@if [ ! -x "$(PY)" ]; then $(MAKE) --no-print-directory bootstrap; fi
	@$(PY) -m pytest -q

test-verbose:
	@$(PY) -m pytest -vv -x

lint:
	@$(PIP) show ruff >/dev/null 2>&1 || $(PIP) install -q ruff
	@$(VENV)/bin/ruff check anna-app/executas/pcb-designer/pcbai tests scripts || true

fmt:
	@$(PIP) show ruff >/dev/null 2>&1 || $(PIP) install -q ruff
	@$(VENV)/bin/ruff format anna-app/executas/pcb-designer/pcbai tests scripts || true

tools:
	@$(PY) -m pcbai.agent.cli tools

backend:
	@$(PY) -m pcbai.agent.cli call capabilities

demo:
	@$(PY) -m pcbai.agent.cli call parse_requirements --arg description="LFE5U-12F FPGA with USB-C, 3.3V LDO and a 12V buck converter"
	@$(PY) -m pcbai.agent.cli call generate_bom --json '{"requirements":{"keywords":["mcu","usb","buck"]}}'
	@$(PY) -m pcbai.agent.cli call generate_footprint --confirm --arg footprint_type=qfp \
	  --json '{"params":{"name":"LQFP-144_TQFP_20x20_P0.5","pins":144,"pitch":0.5,"body_l":20,"body_w":20,"pad_l":1.2,"pad_w":0.25}}' \
	  | $(PY) -c "import json,sys;d=json.load(sys.stdin);print('footprint:',d['data']['path'],'pads',d['data']['pads'])"
	@$(PY) -m pcbai.agent.cli call full_pipeline --confirm --arg description="2-layer ESP32-C3 sensor board" \
	  | $(PY) -c "import json,sys;d=json.load(sys.stdin);print('pipeline mode:',d['data'].get('mode'),'template_only:',d['data'].get('template_only'))"

rpc:
	@$(PY) scripts/test_rpc.py

clean:
	@rm -rf $(ROOT)/build $(ROOT)/.pytest_cache $(ROOT)/*.egg-info
	@find $(ROOT)/anna-app -name '__pycache__' -type d -prune -exec rm -rf {} +
	@echo "cleaned build/ and caches (project sources untouched)"
