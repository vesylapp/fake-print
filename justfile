# Ports come from docker-compose.yml so the firewall rules match what the container listens on.
ipp_port := `grep -oP 'IPP_PORT:\s*"\K[0-9]+' docker-compose.yml || echo 8631`
web_port := `grep -oP 'WEB_PORT:\s*"\K[0-9]+' docker-compose.yml || echo 8080`
raw_port := `grep -oP 'RAW_PORT:\s*"\K[0-9]+' docker-compose.yml || echo 9100`

# List recipes
default:
    @just --list

# Build and start the fake printer
up:
    docker compose up -d --build

# Stop the fake printer
down:
    docker compose down

# Follow the printer's logs
logs:
    docker compose logs -f

# Open the firewall (ufw) so LAN devices can discover and print to the fake printer
firewall-up:
    #!/usr/bin/env bash
    set -euo pipefail
    for rule in {{ ipp_port }}/tcp {{ web_port }}/tcp {{ raw_port }}/tcp 5353/udp; do
        [[ $rule == 0/* ]] && continue  # RAW_PORT "0" means raw printing is off
        echo "allow $rule"
        sudo ufw allow "$rule" comment 'fake-print'
    done
    sudo ufw status | grep -E 'fake-print|^Status'

# Remove the firewall rules added by firewall-up
firewall-down:
    #!/usr/bin/env bash
    set -euo pipefail
    for rule in {{ ipp_port }}/tcp {{ web_port }}/tcp {{ raw_port }}/tcp 5353/udp; do
        [[ $rule == 0/* ]] && continue
        echo "delete allow $rule"
        sudo ufw delete allow "$rule" || true
    done
    sudo ufw status

# Show the firewall rules for the fake printer's ports
firewall-status:
    sudo ufw status | grep -E '^Status|^To|{{ ipp_port }}/tcp|{{ web_port }}/tcp|{{ raw_port }}/tcp|5353/udp' || true
