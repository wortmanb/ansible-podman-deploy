# Elastic Stack on Podman (Ansible)

Ansible deployment of an Elastic Stack (9.5.4) across two rootless Podman hosts
using [Quadlet](https://docs.podman.io/en/latest/markdown/podman-systemd.unit.5.html)
systemd units. It installs only from a local asset cache, so it also works on
air-gapped networks, and can use either its own certificates or ones supplied
by the network's owner.

| Host | Containers | Published ports |
|------|------------|-----------------|
| `podman1.example.com` | `es01`, `es02`, `es03` (all roles) | 9200, 9201, 9202 (HTTPS) |
| `podman2.example.com` | `kibana`, `fleet-server`, `epr`, `nginx` | 5601 (Kibana), 8220 (Fleet), 80/443 (artifact mirror), 8443 (EPR proxy) |

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
inventory/hosts.yml.example  # copy to hosts.yml (gitignored): es_cluster + services hosts
inventory/group_vars/all/
  main.yml                   # defaults: versions, ports, heap, URLs, TLS mode
  overrides.yml.example      # copy to overrides.yml (gitignored) for site settings
  vault.yml.example          # copy to vault.yml (gitignored, encrypted): secrets
roles/
  pki/            # controller: internal CA, then generates or validates the
                  # external certs and stages them in ./pki/external
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
user with sudo, and `podman`, `container-selinux`, `acl` and `ca-certificates`
installable with `dnf` (from a local repository on air-gapped networks).

## Site-specific files

Nothing about a particular site is committed. Each deployment needs these
local files, all gitignored. Create them from the `.example` files next to
them, and keep them (and their backups) with the deployment rather than in git:

| File | Needed | How to create |
|------|--------|---------------|
| `inventory/hosts.yml` | Always | `cp inventory/hosts.yml.example inventory/hosts.yml`, then set the two hostnames (FQDNs clients will use) |
| `inventory/group_vars/all/vault.yml` | Always | `cp ...vault.yml.example ...vault.yml`, set passwords and keys (`openssl rand -hex 32`), then `ansible-vault encrypt` it |
| `inventory/group_vars/all/overrides.yml` | Optional | `cp ...overrides.yml.example ...overrides.yml`, then set anything that differs from `main.yml`: TLS mode, version, heap, ... |
| `certs/` | `tls_mode: provided` | The customer's certificates, keys and CA bundle (see [TLS certificates](#tls-certificates)) |
| `cache/` | Always | `ansible-playbook -i localhost, build-cache.yml` (see [Air-gapped deployment](#air-gapped-deployment)) |
| `pki/` | Created on first run | Internal CA, and the generated certificates in `generated` mode. **Back it up**: losing it means new certs everywhere |

The inventory connects as your local `$USER`; set `ansible_user` in
`hosts.yml` if needed. Certificates and service URLs are derived from the
hostnames there. Hostnames in this README (`podman1.example.com`, ...) are
placeholders.

Put site settings in `overrides.yml` rather than editing `main.yml`, so the
repo stays generic. Files in `group_vars/all/` load in alphabetical order and
later files win, so `overrides.yml` takes precedence over `main.yml`.

## Usage

`site.yml` never downloads anything: images and agent artifacts come from
`cache/`, which `build-cache.yml` fills (see [Air-gapped deployment](#air-gapped-deployment)).
Build the cache first, even on a connected network:

```bash
ansible-playbook -i localhost, build-cache.yml
```

Then create the [site-specific files](#site-specific-files) and deploy:

```bash
ansible-playbook site.yml --ask-vault-pass --ask-become-pass
```

(Drop `--ask-become-pass` if sudo on the hosts is passwordless.)

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

* Kibana: https://podman2.example.com:5601 (user `elastic`)
* Trust `pki/external/ca.crt` in your browser/OS and on enrolling agents. The
  `podman_host` role already adds it to system trust on podman1/podman2
  (disable with `podman_host_trust_ca: false`).
* Enroll agents against `https://podman2.example.com:8220` with
  `--certificate-authorities=/path/to/ca.crt` (a copy of `pki/external/ca.crt`).

## TLS certificates

Every external interface uses a certificate chosen by `tls_mode`:

| Interface | Name | Must be valid for |
|-----------|------|-------------------|
| Elasticsearch HTTP (9200–9202) | `es-http` | the `es_cluster` host (podman1) |
| Kibana (5601) | `kibana` | the `services` host (podman2) |
| Fleet Server (8220) | `fleet-server` | the `services` host (podman2) |
| nginx: artifact mirror (443) and EPR proxy (8443) | `nginx` | the `services` host (podman2) |

* **`generated`** (default): the `pki` role issues all four from its own CA in
  `pki/`. Nothing to supply.
* **`provided`**: use certificates issued by the network's owner.

Traffic between the Elasticsearch nodes always uses the internal CA in `pki/`,
in both modes. It never leaves podman1 and needs per-node certificates.

Either way, the certificates end up in `pki/external/`, and
`pki/external/ca.crt` is what clients must trust. It's installed into system
trust on both hosts and given to Fleet, so agents' Elasticsearch output trusts
it by fingerprint.

### Using provided certificates

**1. Get from the customer** (PEM format):

* A certificate for each host, or for each interface, valid for the hostnames
  in `hosts.yml`, as `DNS:` subject alternative names. Wildcards such as
  `*.example.com` work. With one certificate per host, podman2's covers
  Kibana, Fleet Server and nginx.
* The private key for each, **without a passphrase**.
* The CA bundle that issued them: any intermediates **and the root**.

Common conversions:

```bash
# Remove a key passphrase
openssl pkey -in encrypted.key -out podman2.key

# PFX/.p12 -> leaf-first chain + key (sed strips the "Bag Attributes" text)
openssl pkcs12 -in podman2.pfx -clcerts -nokeys | sed -n '/BEGIN/,/END/p' >  podman2.crt
openssl pkcs12 -in podman2.pfx -cacerts -nokeys | sed -n '/BEGIN/,/END/p' >> podman2.crt
openssl pkcs12 -in podman2.pfx -nocerts -nodes  | sed -n '/BEGIN/,/END/p' >  podman2.key
```

Add `-legacy` to the `pkcs12` commands if OpenSSL 3 rejects an older PFX.
Each `.crt` must hold the leaf certificate first, then its intermediates.
For Elasticsearch, the playbook assembles the served chain itself, in issuer
order and without duplicates, from the `.crt` and `ca.crt` (Java refuses to
start with a misordered chain), so a missing or out-of-order intermediate is
fine as long as `ca.crt` has it.

**2. Put them in `certs/`** (gitignored) at the top of the repo:

```text
certs/
  ca.crt          # CA bundle: intermediates + root
  podman1.crt     # leaf + intermediates, for podman1
  podman1.key
  podman2.crt     # leaf + intermediates, for podman2
  podman2.key
```

```bash
chmod 700 certs && chmod 600 certs/*.key
```

**3. Select them in `inventory/group_vars/all/overrides.yml`:**

```yaml
tls_mode: provided
tls_provided_files:
  es-http: { cert: podman1.crt, key: podman1.key }
  kibana: { cert: podman2.crt, key: podman2.key }
  fleet-server: { cert: podman2.crt, key: podman2.key }
  nginx: { cert: podman2.crt, key: podman2.key }
```

If you name the files after the interfaces instead (`es-http.crt`/`.key`,
`kibana.*`, `fleet-server.*`, `nginx.*`), `tls_mode: provided` alone is enough.
Set `tls_provided_dir` or `tls_provided_ca` if the files live elsewhere or the
bundle has another name.

**4. Run `site.yml`.** Before it touches any host, it checks every
certificate: the file exists, it isn't expired, the key matches, it chains to
the CA bundle, and it covers the required hostname. A failure names the file
and the problem. It then deploys the certificates, restarts the affected
services, and installs the customer CA bundle into both hosts' system trust.

**5. Enroll agents** against `https://podman2.example.com:8220`. If the agent
hosts already trust the customer's CA (usual on a customer network), no extra
flag is needed. Otherwise pass `--certificate-authorities` with a copy of
`certs/ca.crt`.

**Switching an existing deployment** to (or between) provided certificates is
the same procedure. Agents already enrolled stop reaching Fleet Server if they
don't trust the new CA, so switch before enrolling agents, or make sure agent
hosts trust the new CA first.

**Renewing:** replace the files in `certs/` and run `site.yml` again.

**Going back to generated certificates:** remove `tls_mode` from
`overrides.yml` (or set `tls_mode: generated`) and run `site.yml`.

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

It builds for `elastic_version` and the lite EPR image by default. To change
either, set it in `overrides.yml`, which `build-cache.yml` and `site.yml` both
read. You can also pass `-e` for a one-off build, but then set the same values
in `overrides.yml` before deploying, or `site.yml` won't find the images it
expects:

```bash
# different stack version
ansible-playbook -i localhost, build-cache.yml -e elastic_version=9.5.5

# full EPR distribution (~23 GB instead of ~4 GB)
ansible-playbook -i localhost, build-cache.yml -e 'epr_image_tag={{ elastic_version }}'
```

Re-running skips anything already cached. Archives are named by image and
version, so building another version adds to `cache/` rather than replacing it;
delete `cache/` first if you only want to transfer one version.

Then copy the whole repo directory across, including all the gitignored
[site-specific files](#site-specific-files): `cache/`, `inventory/hosts.yml`,
`vault.yml`, `overrides.yml` and `certs/` if you have them, and `pki/` to keep
the same internal CA. Skip `.venv/` and `collections/`; you recreate those
offline:

### On the air-gapped side

```bash
cd ansible-podman-deploy                         # wherever you copied the repo
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
mirror, Fleet service token), the sysctls, firewall ports and the external CA
in system trust. It keeps OS packages and every site-specific file on the
controller, so the next `site.yml` reuses the same certificates and passwords.
Run `site.yml` twice afterwards, as on any fresh install.

## Notes and gotchas

* **PKI**: `pki/` holds the internal CA private key. Back it up. Deleting it
  regenerates every internal (and, in `generated` mode, external) certificate,
  which restarts everything, and enrolled agents must then trust the new CA.
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
