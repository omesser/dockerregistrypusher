# What is this?
This package contains dockerregistrypusher CLI that allows to push image packed as tar (usually from docker save command) to a docker registry
This project was forked from [Adam Raźniewski's dockerregistrypusher](https://github.com/Razikus/dockerregistrypusher)
But with changes and adjustments to iguazio's needs as a CLI
All rights reserved to [Adam](https://github.com/Razikus)

# Why?
To push tar-packed image archives (created by `docker save`) to registries without going through (and taxing) docker-daemon

Usage of CLI:

# Running
```
docker-tar-push {REGISTRYURL} {TARPATH} [login] [password] [--noSslVerify]
```

# License
Free to use (MIT)