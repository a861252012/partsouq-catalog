#!/bin/sh
set -eu

partsouq-catalog-migrate check
exec "$@"
