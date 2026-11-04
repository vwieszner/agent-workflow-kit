---
name: dependency-sync
description: MANDATORY. After adding/removing any package, update the source-controlled manifests and lockfiles immediately, rebuild the app images, and commit the manifest + lockfile together.
---
# Dependency Continuity

## Trigger
Adding or removing any package, in any ecosystem the project uses.

## Action
1. **Immediately** update the source-controlled dependency files (`config: env.dependency_files`)
   with the ecosystem's own tool — the config-editor commands in `config: env.dependency_edit_cmds`
   on the host, or `config: env.container_install_cmd` inside the container. The tool updates the
   manifest and lockfile together.
2. If the app runs in containers, rebuild them against the new lockfile:
   `config: env.dependency_rebuild_cmd`.
3. **Commit the manifest and its lockfile together.** Verify both are in the commit.

Before adding a dependency, apply the ecosystem check in `AGENTS.md`: do not add a package for
something an installed dependency already ships.

## Forbidden
- ❌ Editing a manifest or lockfile by hand (lockfile drifts from manifest).
- ❌ An installing command on the host (`host-passive-environment.md`).
- ❌ Editing the manifest but skipping the image rebuild (the container lacks the package).
- ❌ Installing a package without committing the updated manifest + lockfile.
- ❌ Adding a build toolchain (compiler, build-essential) to the runtime image stage. A package that
  ships no wheel/binary for the platform is built in the base image's builder stage
  (`config: env.base_image_build_cmd`): rebuild and publish the base, then rebuild the app images.
- ❌ A tool-version ceiling in the project's tool-version pin that the kit's `uv run` hooks would
  violate — raise the floor instead, together with any image that pins the same tool.
