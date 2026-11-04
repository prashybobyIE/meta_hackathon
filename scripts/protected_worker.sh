#!/bin/sh
# Start or stop the attested worker in an explicitly configured Azure resource group.
# Bills while up. Starting resources requires separate approval from the operator.
set -eu
set +x
umask 077
SUB="${AZ_SUBSCRIPTION:?Set AZ_SUBSCRIPTION}"
RG="${AZ_RESOURCE_GROUP:?Set AZ_RESOURCE_GROUP}"
NAME="${AZ_CONTAINER_GROUP:?Set AZ_CONTAINER_GROUP}"
cd "$(dirname "$0")/.."
case "${1:-}" in
  up)
    : "${WORKER_IMAGE:?Set WORKER_IMAGE}" "${MODEL_IMAGE:?Set MODEL_IMAGE}" "${CCE_POLICY_FILE:?Set CCE_POLICY_FILE}"
    [ -r "$CCE_POLICY_FILE" ] || { echo "CCE policy file is not readable" >&2; exit 2; }
    PARAMS=$(mktemp "${TMPDIR:-/tmp}/worker-parameters.XXXXXX")
    trap 'rm -f "$PARAMS"' EXIT
    trap 'exit 130' INT
    trap 'exit 143' HUP TERM
    ACR=$(az acr list --subscription "$SUB" -g "$RG" --query '[0].name' -o tsv)
    az acr credential show --subscription "$SUB" -n "$ACR" -o json | python3 scripts/protected_deploy_parameters.py "$PARAMS" "$ACR"
    az deployment group create --subscription "$SUB" -g "$RG" -n "up-$(date +%s)" -f protected_worker/deploy/template.json --parameters "@$PARAMS" -o none
    IP=$(az container show --subscription "$SUB" -g "$RG" -n "$NAME" --query ipAddress.ip -o tsv)
    echo "Worker: http://$IP:8443 (allow about a minute for the model to load)"
    ;;
  down)
    az container delete --subscription "$SUB" -g "$RG" -n "$NAME" -y -o none
    echo "Compute deleted. The registry remains."
    ;;
  *) echo "Usage: $0 up|down" >&2; exit 2 ;;
esac
