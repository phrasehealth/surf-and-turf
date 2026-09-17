# transforms/

Placeholder. Mount your transforms repository here so the agent knows which
tables exist:

    git submodule add git@github.com:your-org/transforms.git workspace/transforms

The Dockerfile copies `workspace/` into the image; the submodule is checked out
at build time (`git submodule update --init` in CI before `docker build`).
