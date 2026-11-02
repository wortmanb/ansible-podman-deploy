# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this is

Ansible deployment of an Elastic Stack onto two rootless Podman hosts using Quadlet (systemd user units), run as the `elastic` service user. `README.md` is thorough (prerequisites, TLS modes, air-gapped flow, gotchas); read it for operator-facing detail. There is no test suite or linter config in the repo.

## Commands

```bash
# Controller setup (Python 3.12-3.14; ansible-core pinned to 2.20.x)
python3 -m venv .venv && . .venv/bin/activate
pip install -r requirements.txt && ansible-galaxy collection install -r requirements.yml

ansible-playbook -i localhost, build-cache.yml            # fill cache/ (needs docker, internet)
ansible-playbook site.yml --ask-vault-pass --ask-become-pass   # deploy (offline, from cache/)
ansible-playbook teardown.yml --ask-vault-pass --ask-become-pass -e teardown_confirm=yes  # destroys data

ansible-playbook site.yml --syntax-check                  # quick validation without hosts
ansible-playbook site.yml --check --diff ...              # dry run
ansible-playbook site.yml --tags/--limit ...              # narrow a run
```

## Architecture

- `site.yml` runs four plays in order: `pki` role on localhost (controller), then `es_cluster` host (podman1: `podman_host` + `elasticsearch`), then `services` host (podman2: `podman_host`, `epr`, `nginx`, `kibana`, `fleet_server`). Role order on podman2 matters (Kibana preconfigures Fleet; Fleet Server needs Kibana/ES up).
- Roles run `become_user: "{{ podman_user }}"` with `environment: "{{ podman_env }}"` (sets `XDG_RUNTIME_DIR`) so containers/systemd `--user` units belong to the rootless `elastic` user. Containers are defined as Quadlet `.container`/`.network` Jinja templates in each role's `templates/`; handlers restart the corresponding user service.
- **Offline-only installs**: `site.yml` never downloads. `build-cache.yml` (+ `tasks/cache-image.yml`) populates the gitignored `cache/` (images, agent artifacts, collections, wheels, SHA256SUMS); the `container_image` role loads an archive onto a host only if the image is missing.
- **PKI role** (controller-side): always creates an internal CA in `pki/` for ES transport certs; external certs (`es-http`, `kibana`, `fleet-server`, `nginx`) are either generated (`tls_mode: generated`, backdated by `pki_backdate`) or validated from `certs/` (`tls_mode: provided`, `validate.yml`/`provided.yml`). Both modes stage results in `pki/external` (`tls_dir`), which other roles copy from. `files/build_chain.py` orders the ES served chain (Java rejects misordered chains).
- **Elasticsearch bootstrap**: `roles/elasticsearch/tasks/main.yml` detects first-time bootstrap by absence of `*-data` podman volumes, sets `elasticsearch_bootstrap`, and tracks `elasticsearch_forming` separately so that `cluster.initial_master_nodes` is removed via rolling restart (`restart.yml`/`restart_node.yml`/`wait_for_cluster.yml`) within the same run. `-e elasticsearch_bootstrap=true` forces it.
- **Fleet** is preconfigured in `roles/kibana/templates/kibana.yml.j2` (read-only in UI); only the agent download source is set via the Fleet API in the kibana role. The Fleet Server service token is created once and persisted on the host.

## Configuration

- Defaults live in `inventory/group_vars/all/main.yml` (versions, ports, heap, URLs, TLS mode). Site settings go in the gitignored `overrides.yml` (loads after `main.yml`, so it wins) — don't edit `main.yml` for site-specific values. Keep the repo generic: hostnames in docs are placeholders.
- Gitignored site files: `inventory/hosts.yml`, `group_vars/all/vault.yml` (encrypted secrets), `overrides.yml`, `certs/`, `cache/`, `pki/`, `collections/`, `.venv`. Copy from the `*.example` files.
- Per-role tunables are in each role's `defaults/main.yml`; image refs are `*_image` variables (changing them requires rebuilding the cache).
- `ansible.cfg` sets `roles_path = roles`, `collections_path = collections`, pipelining on (needed for becoming the unprivileged podman user).

## Conventions

- Commits follow Conventional Commits (`feat(scope):`, `fix(scope):`, `chore(scope):`).
- Targets are RHEL-family 9+ with Podman >= 4.4; managed-host Python may be 3.9, which is why ansible-core is pinned to 2.20.x.

## `rhel8` branch

Targets RHEL 8.10 managed hosts. Modules run under `python3.12` (installed by `bootstrap-rhel8.yml`, which also ships the `selinux` wheel from `cache/python/`), so roles must not use modules needing system-Python bindings (`ansible.builtin.dnf`, `ansible.posix.firewalld`): use `rpm`/`dnf`/`firewall-cmd` commands. `podman_host` also enforces podman >= 4.4, netavark, and cgroup v2 (reboots once via grubby). Not yet tested against a real RHEL 8 host.
