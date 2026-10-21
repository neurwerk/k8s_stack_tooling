# Tooling documentation

Tooling owns setup automation, operational utilities, and the operations container. Base and add-on charts configure its workloads; client repositories select platform releases and settings. Workstation CLIs are separate Python projects and are not installed in the operations image.

- [Architecture](architecture/README.md): components, configuration sources, and execution flows.
- [Development](development/README.md): local environments and repository checks.
- [Operations](operations/README.md): tool selection, execution, and verification.
- [Release adoption](releases.md): standalone CLI source pins and container images.
