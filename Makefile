.PHONY: all test-browser test-pihole status logs takeout-stats

all: status

test-browser:
	python3 src/browser_sync.py

test-pihole:
	ssh ${PIHOLE_HOST:-pihole} "python3 ~/apps/network-tracker/src/pihole_sync.py --max-batches 5"

status:
	@echo "=== Browser History Sync ==="
	@systemctl --user status brain-browser-sync.timer --no-pager || true
	@echo "\n=== Pi-hole DNS Sync ==="
	@ssh ${PIHOLE_HOST:-pihole} "systemctl status brain-pihole-sync.timer --no-pager" || true

logs-browser:
	journalctl --user -u brain-browser-sync.service -n 50 -f

logs-pihole:
	ssh ${PIHOLE_HOST:-pihole} "journalctl -u brain-pihole-sync.service -n 50 -f"

takeout-stats:
	python3 src/takeout_analyzer.py stats

web:
	python3 src/server.py --port 8780

web-status:
	systemctl --user status brain-network-web.service --no-pager

web-logs:
	journalctl --user -u brain-network-web.service -n 50 -f
