# Lab Elastic Stack on Podman

Ansible deployment of an Elastic Stack (9.5.4) across two rootless Podman hosts
using [Quadlet](https://docs.podman.io/en/latest/markdown/podman-systemd.unit.5.html)
systemd units.

| Host | Containers | Published ports |
|------|------------|-----------------|
| `podman1.lab.thewortmans.org` | `es01`, `es02`, `es03` (all roles) | 9200, 9201, 9202 (HTTPS) |
| `podman2.lab.thewortmans.org` | `kibana`, `fleet-server`, `epr`, `nginx` | 5601 (Kibana), 8220 (Fleet), 80/443 (artifact mirror), 8443 (EPR proxy) |

## Why Ansible + Quadlet

* There is no cloud API here, just two hosts, so Ansible handles host config
  (packages, sysctl, users, firewall) and app config in one tool. Terraform's
  Podman providers are immature and can't manage that host config.
* Quadlet turns containers into ordinary systemd user services. They start at
  boot (via linger), restart on failure, and are inspectable with
  `systemctl --user` / `journalctl --user`.
* Everything runs **rootless** as the `elastic` service user.

## Layout

```
site.yml                     # entry point: deploys from cache/
build-cache.yml              # fills cache/ (run on an internet-connected box)
teardown.yml                 # removes everything site.yml created on the hosts
tasks/cache-image.yml        # pull + save one image (used by build-cache.yml)
cache/                       # offline assets; gitignored, see "Air-gapped deployment"
inventory/hosts.yml          # podman1 -> es_cluster, podman2 -> services
inventory/group_vars/        # versions, ports, heap, URLs, secrets (vault)
roles/
  pki/            # controller: self-signed CA + per-service certs in ./pki
  podman_host/    # packages, sysctls, service user, linger, firewalld, CA trust
  container_image/ # loads an image from cache/images/ if the host lacks it
  elasticsearch/  # 3-node cluster, bootstrap detection, rolling restarts
  epr/            # Elastic Package Registry (internal only)
  nginx/          # TLS proxy for EPR (:8443) + artifact mirror (:443)
  kibana/         # Kibana + Fleet preconfiguration
  fleet_server/   # Elastic Agent in Fleet Server mode
```

## Prerequisites

Controller: Python 3.12–3.14 (required by ansible-core 2.20).

```bash
sudo apt install python3-venv python3-pip     # Debian/Ubuntu, if missing
python3 -m venv .venv
. .venv/bin/activate
pip install --upgrade pip
pip install -r requirements.txt
ansible-galaxy collection install -r requirements.yml
```

`requirements.txt` pins ansible-core to 2.20.x because it still supports
Python 3.9 on managed hosts (RHEL 9's system Python). Check the
[support matrix](https://docs.ansible.com/projects/ansible/latest/reference_appendices/release_and_maintenance.html)
before raising the pin. `cryptography` is needed locally by the `pki` role.

Targets: RHEL-family 9+ with Podman >= 4.4 (5.x recommended), SSH access as a
user with sudo. The inventory connects as your local `$USER`; override
`ansible_user` in `inventory/hosts.yml` if needed.

## Usage

`site.yml` never downloads anything: images and agent artifacts come from
`cache/`, which `build-cache.yml` fills (see [Air-gapped deployment](#air-gapped-deployment)).
Build the cache first, even on a connected network:

```bash
ansible-playbook -i localhost, build-cache.yml
```

Then:

```bash
cp inventory/group_vars/all/vault.yml.example inventory/group_vars/all/vault.yml
$EDITOR inventory/group_vars/all/vault.yml        # set passwords; keys via `openssl rand -hex 32`
ansible-vault encrypt inventory/group_vars/all/vault.yml

ansible-playbook site.yml --ask-vault-pass
```

Run the playbook **twice** on a fresh install. The first run bootstraps the
cluster with `cluster.initial_master_nodes`. The second run sees the existing
data volumes, removes that setting (as Elastic recommends), and does a rolling
restart.

If the first run fails before the cluster forms, the data volumes already exist,
so later runs won't bootstrap. Either delete the volumes (see below) or force
bootstrap with `-e elasticsearch_bootstrap=true`.

```bash
# as the elastic user on podman1 -- destroys all ES data
systemctl --user stop es01 es02 es03
podman volume rm es01-data es02-data es03-data
```

Afterwards:

* Kibana: https://podman2.lab.thewortmans.org:5601 (user `elastic`)
* Trust `pki/ca.crt` in your browser/OS and on enrolling agents. The
  `podman_host` role already adds it to system trust on podman1/podman2
  (disable with `podman_host_trust_ca: false`).
* Enroll agents against `https://podman2.lab.thewortmans.org:8220` with
  `--certificate-authorities=/path/to/ca.crt`.

## Air-gapped deployment

`build-cache.yml` collects everything the deployment needs into `cache/`:

| Path | Contents |
|------|----------|
| `cache/images/` | ES, Kibana, Elastic Agent, EPR (lite), nginx images (`docker save` archives, linux/amd64) |
| `cache/artifacts/` | Elastic Agent packages for the mirror (+ `.sha512`, `.asc`) and `GPG-KEY-elasticsearch` |
| `cache/collections/` | Ansible collection tarballs from `requirements.yml` |
| `cache/python/` | Controller wheels from `requirements.txt` for CPython 3.12–3.14, Linux x86_64 |
| `cache/SHA256SUMS` | Checksums of all of the above |

Agent checksums and GPG signatures are verified during the build. What's
mirrored is controlled by `nginx_artifact_mirror_platforms` (nginx role); the
image list by each role's `*_image` variable.

**Not cached:** OS packages (`podman`, `container-selinux`, `acl`,
`ca-certificates`) still install with `dnf`, so the air-gapped hosts need a
local RHEL/EL repository. Elastic Defend/Endpoint artifacts aren't mirrored.

### On the connected side

```bash
ansible-playbook -i localhost, build-cache.yml   # needs docker, ~7 GB of downloads
```

It builds for `elastic_version` and the lite EPR image by default. Override
either with `-e`, and set the same values in `inventory/group_vars/all/main.yml`
before deploying, or `site.yml` won't find the images it expects:

```bash
# different stack version
ansible-playbook -i localhost, build-cache.yml -e elastic_version=9.5.5

# full EPR distribution (~23 GB instead of ~4 GB)
ansible-playbook -i localhost, build-cache.yml -e 'epr_image_tag={{ elastic_version }}'
```

Re-running skips anything already cached. Archives are named by image and
version, so building another version adds to `cache/` rather than replacing it;
delete `cache/` first if you only want to transfer one version.

Then copy the whole repo directory across, including the gitignored `cache/`,
plus `pki/` and `vault.yml` if you want to keep the same CA and secrets.

### On the air-gapped side

```bash
cd aerospace
(cd cache && sha256sum -c --quiet SHA256SUMS)    # verify the transfer

python3 -m venv .venv && . .venv/bin/activate
pip install --no-index --find-links cache/python -r requirements.txt
(cd cache/collections && ansible-galaxy collection install --offline -r requirements.yml -p ../../collections)

ansible-playbook site.yml --ask-vault-pass --ask-become-pass
```

`site.yml` copies each image archive to a host only when that image is
missing there, loads it with `podman load`, and deletes the archive.

## Starting over

`teardown.yml` returns the hosts to "OS + podman packages installed" so you can
test a fresh deploy (for example, the air-gapped path with outbound internet
blocked). It **destroys all Elastic data**, so it refuses to run without
confirmation:

```bash
ansible-playbook teardown.yml --ask-vault-pass --ask-become-pass -e teardown_confirm=yes
```

It removes the service user's containers, images, volumes and networks, its
quadlets, the `elastic` user and home directory (certs, configs, artifact
mirror, Fleet service token), the sysctls, firewall ports and the lab CA in
system trust. It keeps OS packages, and `pki/`, `vault.yml` and `cache/` on the
controller, so the next `site.yml` reuses the same CA and passwords. Run
`site.yml` twice afterwards, as on any fresh install.

## Notes and gotchas

* **PKI**: `pki/` holds the CA private key and is gitignored. Back it up.
  Deleting it regenerates every cert, which means a restart of everything.
* **Fleet is preconfigured** in `kibana.yml` (Fleet Server host, ES output with
  CA fingerprint, Fleet Server policy). These objects are read-only in the UI.
  Change them in `roles/kibana/templates/kibana.yml.j2`. The agent download
  source can't be preconfigured, so the kibana role sets it via the Fleet API
  (it stays editable in the UI, but the next playbook run resets it).
* **EPR**: Kibana reaches EPR directly at `http://epr:8080` on the podman
  network. nginx exposes it at `https://podman2...:8443` for external use. Set
  `kibana_registry_url: "{{ epr_public_url }}"` to send Kibana through nginx.
  The `lite` image (~4 GB) is the default. Set `epr_image_tag:
  "{{ elastic_version }}"` for the full distribution (~23 GB) and rebuild the
  cache.
* **Artifact mirror**: the nginx role copies Elastic Agent packages
  (+ `.sha512`/`.asc`) for `nginx_artifact_mirror_platforms` into
  `/home/elastic/artifacts/downloads/` from `cache/artifacts/`, served at
  `/downloads/`. That URL is
  configured as Fleet's default agent binary download source. Drop other files
  you want to serve into `/home/elastic/artifacts/`.
* **Fleet Server service token** is created once and stored at
  `/home/elastic/fleet-server/service-token`. Delete that file to rotate it.
* **`elastic` password**: `ELASTIC_PASSWORD` only seeds the password on first
  start. Changing it in vault later requires changing it in ES too (API or
  `elasticsearch-reset-password`).
* **Rootless low ports**: podman2 gets `net.ipv4.ip_unprivileged_port_start=80`
  so nginx can bind 80/443.
* **Memory**: three ES nodes at `es_heap: 2g` need ~12 GB+ RAM on podman1.
* Rolling restarts wait for all nodes and yellow health between nodes, but
  don't disable shard allocation. That's fine for a lab, but do it manually
  for big clusters.

## Operating

```bash
ssh podman1 && sudo -iu elastic
export XDG_RUNTIME_DIR=/run/user/$(id -u)   # sudo does not set it
systemctl --user status es01 es02 es03
journalctl --user -u es01 -f
podman ps
```
