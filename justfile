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
    python3 scripts/firewall.py up {{ ipp_port }}/tcp {{ web_port }}/tcp {{ raw_port }}/tcp 5353/udp

# Remove only rules tagged by firewall-up (even if configured ports changed)
firewall-down:
    python3 scripts/firewall.py down

# Show the firewall rules for the fake printer's ports
firewall-status:
    sudo ufw status
