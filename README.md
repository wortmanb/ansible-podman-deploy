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
site.yml                     # entry point
inventory/hosts.yml          # podman1 -> es_cluster, podman2 -> services
inventory/group_vars/        # versions, ports, heap, URLs, secrets (vault)
roles/
  pki/            # controller: self-signed CA + per-service certs in ./pki
  podman_host/    # packages, sysctls, service user, linger, firewalld
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
* Trust `pki/ca.crt` in your browser/OS and on enrolling agents.
* Enroll agents against `https://podman2.lab.thewortmans.org:8220` with
  `--certificate-authorities=/path/to/ca.crt`.

## Notes and gotchas

* **PKI**: `pki/` holds the CA private key and is gitignored. Back it up.
  Deleting it regenerates every cert, which means a restart of everything.
* **Fleet is preconfigured** in `kibana.yml` (Fleet Server host, ES output with
  CA fingerprint, agent download source, Fleet Server policy). These objects
  are read-only in the UI. Change them in `roles/kibana/templates/kibana.yml.j2`.
* **EPR**: Kibana reaches EPR directly at `http://epr:8080` on the podman
  network. nginx exposes it at `https://podman2...:8443` for external use. Set
  `kibana_registry_url: "{{ epr_public_url }}"` to send Kibana through nginx.
  The full `distribution` image is very large. Set `epr_image_tag:
  "lite-{{ elastic_version }}"` for a smaller one.
* **Artifact mirror**: the nginx role downloads Elastic Agent packages
  (+ `.sha512`/`.asc`) for `nginx_artifact_mirror_platforms` into
  `/home/elastic/artifacts/downloads/`, served at `/downloads/`. That URL is
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
