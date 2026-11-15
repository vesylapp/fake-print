# Fake IPP Printer

A Docker container that pretends to be a network printer. It speaks IPP (IPP Everywhere / AirPrint
compatible), advertises itself over Bonjour/mDNS, accepts print jobs, "prints" them at a configurable
speed, and saves every job as a PDF. A live web dashboard shows jobs as they arrive and lets you open
them.

## Run

```sh
docker compose up -d --build
```

Or use the prebuilt image, which GitHub Actions publishes on every push to `main`
(`linux/amd64` and `linux/arm64`, so it also runs on a Raspberry Pi). Tags are `latest` and
`sha-<short commit>`:

```sh
docker run -d \
  --name fake-printer \
  --restart unless-stopped \
  --network host \
  -e PRINTER_NAME="Fake IPP Printer" \
  -e SECONDS_PER_PAGE=2 \
  -e IPP_PORT=8631 \
  -e WEB_PORT=8080 \
  -e RAW_PORT=9100 \
  -e MEDIA_DEFAULT=na_letter_8.5x11in \
  -v "$PWD/print-jobs:/data" \
  ghcr.io/vesylapp/fake-print:latest
```

- `--network host` is needed for Bonjour/AirPrint discovery and only works on Linux. To print by
  address only, use `-p 8631:8631 -p 8080:8080 -p 9100:9100 -e MDNS=false` instead.
- Any setting from the [Configuration](#configuration-environment-variables-in-docker-composeyml)
  table can be passed with `-e`.
- If the package is private, log in first with a GitHub token that has `read:packages`:
  `echo "$GITHUB_TOKEN" | docker login ghcr.io -u <github-username> --password-stdin`
- Update with `docker pull ghcr.io/vesylapp/fake-print:latest`, then remove and re-run the
  container. Stop it with `docker rm -f fake-printer`.

To use the prebuilt image with compose instead, replace `build: .` in `docker-compose.yml` with
`image: ghcr.io/vesylapp/fake-print:latest`.

- **Dashboard:** http://localhost:8080/
- **Printer URI:** `ipp://<this-machine-ip>:8631/ipp/print` (the dashboard header lists the exact URIs)
- **PDFs:** `./print-jobs/jobs/<id>/output.pdf`. The original bytes the client sent are saved next to
  it as `document-N.<ext>`, and the job metadata as `job.json`.

## Configuration (environment variables in `docker-compose.yml`)

| Variable           | Default               | Notes |
|--------------------|-----------------------|-------|
| `SECONDS_PER_PAGE` | `2`                   | Print speed. Decimals allowed; `0` = instant. You can also change it live in the dashboard (live changes reset on restart). Copies count as pages: a 3-page job with 2 copies takes 6 × this. |
| `IPP_PORT`         | `8631`                | Set to `631` (the standard IPP port) if nothing else on the host uses it. CUPS normally does on desktop Linux. |
| `WEB_PORT`         | `8080`                | Dashboard port. |
| `PRINTER_NAME`     | `Fake IPP Printer`    | Name shown in mDNS and IPP. |
| `PRINTER_LOCATION` | `Docker`              | |
| `MEDIA_DEFAULT`    | `na_letter_8.5x11in`  | Paper loaded at startup; switch it live with the **Media** dropdown in the dashboard (resets on restart). Options: `na_letter_8.5x11in`, `na_legal_8.5x14in`, `iso_a4_210x297mm`, `iso_a5_148x210mm`, `na_index-4x6_4x6in`, `oe_4x6-label_4x6in`, `na_number-10_4.125x9.5in`. The printer reports it as `media-ready`/`media-default`, and text, ZPL and unrecognized jobs are laid out on it. |
| `LABELARY`         | `true`                | Render ZPL jobs with the [Labelary API](https://labelary.com/service.html). See [ZPL labels](#zpl-labels). |
| `ZPL_DPMM`         | `8`                   | ZPL print density in dots per mm: `6`, `8` (203 dpi), `12` (300 dpi) or `24` (600 dpi). Must match the density the ZPL was written for. |
| `LABELARY_URL`     | `https://api.labelary.com` | Change this if you have a Labelary premium or on-premise server. |
| `COLOR` / `DUPLEX` | `true`                | Capabilities to advertise. |
| `MDNS`             | `true`                | Bonjour advertisement (`_ipp._tcp`, plus `_universal` and `_print` subtypes for AirPrint). |

The compose file uses `network_mode: host` because mDNS discovery needs it. If you only need to
reach the printer by URI, you can drop that and publish ports instead
(`ports: ["8631:8631", "8080:8080"]`, `MDNS: "false"`).

**Firewall:** if the host runs `ufw`, allow the ports (and mDNS for discovery) so other devices can
reach the printer:

```sh
just firewall-up      # allows the IPP, web and raw ports from docker-compose.yml, plus mDNS (5353/udp)
just firewall-down    # removes those rules again
just firewall-status
```

The `justfile` also has `just up`, `just down` and `just logs`.

## What it supports

- **Operations:** Print-Job, Validate-Job, Create-Job, Send-Document (multi-document jobs),
  Close-Job, Cancel-Job, Cancel-My-Jobs, Get-Job-Attributes, Get-Jobs, Get-Printer-Attributes,
  Identify-Printer (flashes the dashboard).
- **Document formats:** PDF, PostScript (via Ghostscript), PWG Raster, Apple URF raster (what iOS
  sends), JPEG, PNG, plain text, and `application/octet-stream` (auto-detected). Unknown binary data
  still completes and produces a placeholder PDF with a hex dump; the raw bytes are kept.
- **Compression:** `gzip` and `deflate`.
- `page-ranges` is applied to the output PDF. Other job attributes (media, sides, copies,
  color mode, etc.) are recorded and shown in the dashboard so you can check what your server sent.
- Passes the CUPS `ipptool` conformance suite (`ipp-everywhere.test`) with no failures.

## ZPL labels

ZPL jobs (from IPP or raw port 9100) are rendered by Labelary's free online API at the loaded
media size (pick "4 × 6 in label" in the Media dropdown for shipping labels), one PDF page per label.
Labelary's linter also runs, and any ZPL warnings are shown on the job in the dashboard.

- **The label data leaves your network.** Every ZPL job is sent to api.labelary.com, and the free
  plan has no zero-data-retention guarantee. Set `LABELARY: "false"` if your labels contain data
  that shouldn't be sent there.
- **Free-plan limits:** 3 requests/second, 5,000 per day, 50 labels and 1 MB per request. Large
  jobs are split into batches automatically, and requests are paced to stay under the rate limit.
- **If Labelary can't render a job** (offline, daily limit, invalid ZPL), the job still completes
  with the ZPL source as text, and the reason is shown on the job.

## Simulating printer problems

The dashboard header has **Pause** and **Out of paper** toggles. Either one moves the printer to
`stopped` (`paused` or `media-empty-error`/`media-needed-error` in `printer-state-reasons`). The
current job goes to `processing-stopped` and continues once you clear the condition. Use these to
test how your cloud print server handles printer errors.

## Quick test from the command line

```sh
ipptool -tv -f some.pdf -d filetype=application/pdf ipp://localhost:8631/ipp/print \
  /usr/share/cups/ipptool/print-job.test
```

Or use **Print file…** in the dashboard.

## HTTP API

| Method & path                  | Purpose |
|--------------------------------|---------|
| `GET /api/state`               | Printer status and all jobs (JSON) |
| `GET /api/events`              | Server-Sent Events stream of job and printer updates |
| `POST /api/settings`           | JSON with any of `seconds_per_page`, `media`, `paused`, `out_of_paper`, `accepting` |
| `POST /api/upload`             | Multipart file upload; queues it as a job |
| `POST /api/jobs/{id}/cancel`   | Cancel a job |
| `DELETE /api/jobs/{id}`        | Delete a job and its files |
| `POST /api/clear`              | Delete all finished jobs |
| `GET /jobs/{id}/pdf`           | The rendered PDF |
| `GET /jobs/{id}/documents/{n}` | The original document as received |

`/api/settings` makes it easy to script error scenarios in automated tests, for example
`curl -X POST localhost:8080/api/settings -d '{"out_of_paper": true}'`.
