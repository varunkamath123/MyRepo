#!/usr/bin/env bash
# =============================================================================
# enable_ipv6_ec2.sh — give the FnO_T_Bot EC2 host a reachable IPv6 address
# =============================================================================
#
# WHY
#   The host is IPv4-only. Twice now the ISP's IPv4 path has died one hop past
#   223.178.56.1 while IPv6 stayed healthy (ping ::8888 = 0% loss, 10ms), which
#   blinds us to the bot for a day or more at a time. AWS's own API sits on the
#   same broken path, so SSM is not a workaround during an outage either.
#   IPv6 is additive: nothing here removes or changes any IPv4 setting, so the
#   existing SSH route keeps working exactly as it does today.
#
# SAFETY
#   Read-only by default. It prints what it WOULD change and exits.
#   Pass --apply to actually make changes. Every step is idempotent, so it is
#   safe to re-run (also the way to refresh the SG rule if your ISP rotates
#   your IPv6 prefix).
#
# NOT TESTED AGAINST LIVE AWS
#   Written during an IPv4 outage with no access to the account, so the calls
#   could not be exercised. Run --check first and read its output before
#   --apply. Nothing in --check mutates anything.
#
# USAGE
#   bash scripts/enable_ipv6_ec2.sh --check     # report only (default)
#   bash scripts/enable_ipv6_ec2.sh --apply     # make the changes
#
# REQUIRES
#   aws CLI v2, configured credentials with EC2 network permissions.
#   If `aws sts get-caller-identity` fails, run `aws configure` first — the
#   credentials on this machine were already known to be expired.
# =============================================================================
set -uo pipefail

HOST_IP="${HOST_IP:-3.108.16.113}"
REGION="${AWS_REGION:-ap-south-1}"
SSH_KEY="${SSH_KEY:-$HOME/.ssh/fno-t-bot-new-key}"
MODE="check"
[[ "${1:-}" == "--apply" ]] && MODE="apply"

say()  { printf '%s\n' "$*"; }
step() { printf '\n\033[1m== %s\033[0m\n' "$*"; }
would(){ if [[ $MODE == apply ]]; then printf '   APPLY: %s\n' "$*"; else printf '   would: %s\n' "$*"; fi; }
run()  { if [[ $MODE == apply ]]; then "$@"; else say "   (skipped, --check)"; fi; }

step "0. preflight"
command -v aws >/dev/null || { say "aws CLI not found — install it first"; exit 1; }
if ! aws sts get-caller-identity --region "$REGION" >/dev/null 2>&1; then
  say "AWS credentials are not working. Run:  aws configure"
  say "(this was already a known blocker for SSM)"
  exit 1
fi
say "   credentials OK, region $REGION, mode=$MODE"

step "1. locate the instance by its public IPv4"
INST_JSON=$(aws ec2 describe-instances --region "$REGION" \
  --filters "Name=ip-address,Values=$HOST_IP" \
  --query 'Reservations[].Instances[]' --output json 2>/dev/null)
IID=$(echo "$INST_JSON"  | grep -o '"InstanceId": "[^"]*"'        | head -1 | cut -d'"' -f4)
VPC=$(echo "$INST_JSON"  | grep -o '"VpcId": "[^"]*"'             | head -1 | cut -d'"' -f4)
SUBNET=$(echo "$INST_JSON"| grep -o '"SubnetId": "[^"]*"'         | head -1 | cut -d'"' -f4)
ENI=$(echo "$INST_JSON"  | grep -o '"NetworkInterfaceId": "[^"]*"'| head -1 | cut -d'"' -f4)
SG=$(echo "$INST_JSON"   | grep -o '"GroupId": "[^"]*"'           | head -1 | cut -d'"' -f4)
[[ -z "$IID" ]] && { say "   no instance found with public IP $HOST_IP"; exit 1; }
say "   instance $IID  vpc $VPC  subnet $SUBNET"
say "   eni $ENI  sg $SG"

step "2. VPC IPv6 CIDR"
V6VPC=$(aws ec2 describe-vpcs --region "$REGION" --vpc-ids "$VPC" \
  --query 'Vpcs[0].Ipv6CidrBlockAssociationSet[0].Ipv6CidrBlock' --output text 2>/dev/null)
if [[ "$V6VPC" == "None" || -z "$V6VPC" ]]; then
  would "associate an Amazon-provided /56 with $VPC"
  run aws ec2 associate-vpc-cidr-block --region "$REGION" \
      --vpc-id "$VPC" --amazon-provided-ipv6-cidr-block
  [[ $MODE == apply ]] && sleep 8
  V6VPC=$(aws ec2 describe-vpcs --region "$REGION" --vpc-ids "$VPC" \
    --query 'Vpcs[0].Ipv6CidrBlockAssociationSet[0].Ipv6CidrBlock' --output text 2>/dev/null)
fi
say "   vpc IPv6: $V6VPC"

step "3. subnet IPv6 /64"
V6SUB=$(aws ec2 describe-subnets --region "$REGION" --subnet-ids "$SUBNET" \
  --query 'Subnets[0].Ipv6CidrBlockAssociationSet[0].Ipv6CidrBlock' --output text 2>/dev/null)
if [[ "$V6SUB" == "None" || -z "$V6SUB" ]]; then
  # carve the first /64 out of the VPC's /56
  BASE="${V6VPC%::*}"; CAND="${BASE}::/64"
  would "assign $CAND to subnet $SUBNET"
  run aws ec2 associate-subnet-cidr-block --region "$REGION" \
      --subnet-id "$SUBNET" --ipv6-cidr-block "$CAND"
  V6SUB="$CAND"
fi
say "   subnet IPv6: $V6SUB"
would "enable auto-assign IPv6 on new ENIs in $SUBNET"
run aws ec2 modify-subnet-attribute --region "$REGION" \
    --subnet-id "$SUBNET" --assign-ipv6-address-on-creation

step "4. default IPv6 route to the internet gateway"
RTB=$(aws ec2 describe-route-tables --region "$REGION" \
  --filters "Name=association.subnet-id,Values=$SUBNET" \
  --query 'RouteTables[0].RouteTableId' --output text 2>/dev/null)
[[ "$RTB" == "None" || -z "$RTB" ]] && RTB=$(aws ec2 describe-route-tables --region "$REGION" \
  --filters "Name=vpc-id,Values=$VPC" "Name=association.main,Values=true" \
  --query 'RouteTables[0].RouteTableId' --output text 2>/dev/null)
IGW=$(aws ec2 describe-internet-gateways --region "$REGION" \
  --filters "Name=attachment.vpc-id,Values=$VPC" \
  --query 'InternetGateways[0].InternetGatewayId' --output text 2>/dev/null)
say "   route table $RTB, igw $IGW"
if aws ec2 describe-route-tables --region "$REGION" --route-table-ids "$RTB" \
     --query 'RouteTables[0].Routes[?DestinationIpv6CidrBlock==`::/0`]' \
     --output text 2>/dev/null | grep -q .; then
  say "   ::/0 route already present"
else
  would "add ::/0 -> $IGW on $RTB"
  run aws ec2 create-route --region "$REGION" --route-table-id "$RTB" \
      --destination-ipv6-cidr-block ::/0 --gateway-id "$IGW"
fi

step "5. security group — SSH over IPv6 from YOUR prefix only"
# Deliberately NOT ::/0. Opening 22 to the whole IPv6 internet to fix a
# connectivity annoyance would be a straight security downgrade.
MYV6=$(curl -6 -s -m 10 https://ifconfig.co 2>/dev/null || curl -6 -s -m 10 https://api64.ipify.org 2>/dev/null)
if [[ -z "$MYV6" ]]; then
  say "   could not detect your public IPv6 — re-run this step when you can."
  say "   Then:  aws ec2 authorize-security-group-ingress --region $REGION \\"
  say "            --group-id $SG --ip-permissions \\"
  say "            'IpProtocol=tcp,FromPort=22,ToPort=22,Ipv6Ranges=[{CidrIpv6=YOUR/64,Description=home-v6}]'"
else
  PREFIX="$(echo "$MYV6" | cut -d: -f1-4)::/64"
  say "   your IPv6: $MYV6  -> allowing $PREFIX"
  say "   NOTE: residential IPv6 prefixes can rotate. If SSH over v6 stops"
  say "         working later, just re-run this script to refresh the rule."
  would "authorize tcp/22 from $PREFIX on $SG"
  run aws ec2 authorize-security-group-ingress --region "$REGION" \
      --group-id "$SG" --ip-permissions \
      "IpProtocol=tcp,FromPort=22,ToPort=22,Ipv6Ranges=[{CidrIpv6=$PREFIX,Description=home-v6}]"
fi

step "6. give the instance an IPv6 address"
HAS=$(aws ec2 describe-network-interfaces --region "$REGION" \
  --network-interface-ids "$ENI" \
  --query 'NetworkInterfaces[0].Ipv6Addresses[0].Ipv6Address' --output text 2>/dev/null)
if [[ "$HAS" == "None" || -z "$HAS" ]]; then
  would "assign one IPv6 address to $ENI"
  run aws ec2 assign-ipv6-addresses --region "$REGION" \
      --network-interface-id "$ENI" --ipv6-address-count 1
  [[ $MODE == apply ]] && sleep 5
  HAS=$(aws ec2 describe-network-interfaces --region "$REGION" \
    --network-interface-ids "$ENI" \
    --query 'NetworkInterfaces[0].Ipv6Addresses[0].Ipv6Address' --output text 2>/dev/null)
fi
say "   instance IPv6: $HAS"

step "DONE"
if [[ $MODE != apply ]]; then
  say "   This was a dry run. Re-run with --apply to make the changes."
  exit 0
fi
if [[ "$HAS" != "None" && -n "$HAS" ]]; then
  say "   Test it:   ssh -6 -i \"$SSH_KEY\" ec2-user@$HAS 'hostname; uptime'"
  say ""
  say "   Then add to ~/.ssh/config so the v6 path is one word:"
  say "       Host fnobot"
  say "           HostName $HAS"
  say "           User ec2-user"
  say "           IdentityFile $SSH_KEY"
  say "           AddressFamily inet6"
  say ""
  say "   The instance may also need IPv6 brought up inside the OS:"
  say "       sudo dhclient -6 -v eth0    # Amazon Linux 2"
  say "   (usually automatic; check with 'ip -6 addr show')"
fi
