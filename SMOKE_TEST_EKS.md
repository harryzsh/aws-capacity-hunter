# EKS Self-Managed Node Group Smoke Test（美东 us-east-1）

**目标**：在 `<ACCOUNT_ID>` / `us-east-1` 端到端验证——
1. `grab_odcr.py` 能**真抢到** i4i.16xlarge 的 ODCR；
2. 一个**全新独立的 EKS 集群 + self-managed node group（每 AZ 一个 ASG）** 能把这些预留**吃进去**、节点 `Ready` 入集群、Pod 调度上去。

> **「成功」的定义**：ODCR 抢到（`--list` 显示 active）→ self-managed node 起来并 `kubectl get nodes` 为 `Ready` → 实例的 `CapacityReservationId` 命中我们抢的预留（预留 `Available` 被占满）→ 测试 Pod `Running`。

---

## 🔴 头号铁律：绝不触碰生产 `litellm-cluster`

生产集群：`arn:aws:eks:us-east-1:<ACCOUNT_ID>:cluster/litellm-cluster`。本测试与它**零交集**，靠以下硬隔离保证：

| 隔离维度 | 本测试用 | 生产 | 怎么保证不串 |
|---|---|---|---|
| **EKS 集群名** | `i4i-smoke-eks` | `litellm-cluster` | 名字完全不同；所有命令显式写死集群名，绝不用变量默认值 |
| **VPC** | **新建专用 VPC**（`10.99.0.0/16`） | 各自独立 | 不复用默认 VPC、不碰生产 VPC，物理隔离 |
| **资源 tag** | `purpose=i4i-smoke-eks` | — | 所有 create 都打这个 tag；所有 delete 都按这个 tag 过滤 |
| **ODCR tag** | `capacity-reservation`（脚本默认） | — | `--cancel-all` 只取消这个 tag 的预留 |
| **IAM/账号** | 同账号 `<ACCOUNT_ID>` | 同账号 | open 预留需同账号——这是唯一共享面，但预留是新建的、tag 独立 |

> ⚠️ **每一条 `delete` / `cancel` 命令执行前，先肉眼确认命令里的集群名是 `i4i-smoke-eks`、tag 是 `i4i-smoke-eks` 或 `capacity-reservation`。** 凡是命令里出现 `litellm` 字样 —— 立刻停手，这不是本测试该碰的东西。
>
> ⚠️ 本测试**不使用任何 `--all` / 无过滤的批量删除**。所有清理都按本测试专属 tag 精确删。

---

## 规模与成本

- 机型 `i4i.16xlarge`（64 vCPU/台），**两 AZ 各 1 台 = 共 2 台 = 128 vCPU**（验机制，不验规模）。
- i4i.16xlarge ≈ $5.491/h·台 → 2 台跑 ~20 分钟 ≈ **$3.7**。ODCR 一建即计费，**验完立刻拆**。
- 还需一个 EKS 控制面：**$0.10/h**，跑 1 小时内忽略不计。
- 前提：账号 On-Demand Standard vCPU 配额 ≥ 128（验证规模小，生产 1 万核的配额工单是另一回事）。

---

## 环境变量（先设好，后面命令都引用）

```bash
export AWS_PROFILE=<你的账号 profile>
export AWS_REGION=us-east-1
export CLUSTER=i4i-smoke-eks            # ← 测试集群名，绝不等于 litellm-cluster
export TAG=i4i-smoke-eks               # ← 测试资源统一 tag
export AZ1=us-east-1b
export AZ2=us-east-1d
export ACCOUNT_ID=<你的账号 ID>        # 用于开跑前断言

# 开跑前断言：当前账号确实是目标账号，否则停手
test "$(aws sts get-caller-identity --query Account --output text)" = "$ACCOUNT_ID" \
  && echo "account OK" || { echo "WRONG ACCOUNT — STOP"; }
```

---

## 阶段 A：建独立网络（新 VPC，不碰任何现有 VPC）

```bash
# A1. 新建专用 VPC 10.99.0.0/16
VPC=$(aws ec2 create-vpc --cidr-block 10.99.0.0/16 \
  --tag-specifications "ResourceType=vpc,Tags=[{Key=purpose,Value=$TAG},{Key=Name,Value=$TAG}]" \
  --query 'Vpc.VpcId' --output text)
aws ec2 modify-vpc-attribute --vpc-id $VPC --enable-dns-hostnames '{"Value":true}'
echo "VPC=$VPC"

# A2. IGW + 路由（节点要拉镜像/连 EKS 端点）
IGW=$(aws ec2 create-internet-gateway \
  --tag-specifications "ResourceType=internet-gateway,Tags=[{Key=purpose,Value=$TAG}]" \
  --query 'InternetGateway.InternetGatewayId' --output text)
aws ec2 attach-internet-gateway --vpc-id $VPC --internet-gateway-id $IGW
RT=$(aws ec2 create-route-table --vpc-id $VPC \
  --tag-specifications "ResourceType=route-table,Tags=[{Key=purpose,Value=$TAG}]" \
  --query 'RouteTable.RouteTableId' --output text)
aws ec2 create-route --route-table-id $RT --destination-cidr-block 0.0.0.0/0 --gateway-id $IGW

# A3. 两个公有子网（每 AZ 一个，对齐"每 AZ 一个 ASG"）
SUB1=$(aws ec2 create-subnet --vpc-id $VPC --cidr-block 10.99.1.0/24 --availability-zone $AZ1 \
  --tag-specifications "ResourceType=subnet,Tags=[{Key=purpose,Value=$TAG}]" \
  --query 'Subnet.SubnetId' --output text)
SUB2=$(aws ec2 create-subnet --vpc-id $VPC --cidr-block 10.99.2.0/24 --availability-zone $AZ2 \
  --tag-specifications "ResourceType=subnet,Tags=[{Key=purpose,Value=$TAG}]" \
  --query 'Subnet.SubnetId' --output text)
aws ec2 modify-subnet-attribute --subnet-id $SUB1 --map-public-ip-on-launch
aws ec2 modify-subnet-attribute --subnet-id $SUB2 --map-public-ip-on-launch
aws ec2 associate-route-table --route-table-id $RT --subnet-id $SUB1
aws ec2 associate-route-table --route-table-id $RT --subnet-id $SUB2
echo "SUB1=$SUB1 SUB2=$SUB2"
```

---

## 阶段 B：建独立 EKS 集群（控制面）

用 `eksctl` 最省事。**集群名写死 `i4i-smoke-eks`，nodeGroup 留空**（我们要自管 self-managed node group，不让 eksctl 建托管组）。

```bash
cat > /tmp/$CLUSTER.yaml <<EOF
apiVersion: eksctl.io/v1alpha5
kind: ClusterConfig
metadata:
  name: ${CLUSTER}            # i4i-smoke-eks —— 不是 litellm-cluster
  region: ${AWS_REGION}
  tags: { purpose: ${TAG} }
vpc:
  id: ${VPC}
  subnets:
    public:
      ${AZ1}: { id: ${SUB1} }
      ${AZ2}: { id: ${SUB2} }
# 不在这里定义 nodeGroups —— 控制面 only，节点用 self-managed ASG 自己接
EOF

eksctl create cluster -f /tmp/$CLUSTER.yaml   # ~15 分钟
aws eks update-kubeconfig --name $CLUSTER --region $AWS_REGION
kubectl config current-context                # 确认上下文里是 i4i-smoke-eks
kubectl get svc                               # 通了说明控制面 OK
```

> 验证隔离：`kubectl config current-context` 必须含 `i4i-smoke-eks`。**若显示 litellm-cluster，立刻 `kubectl config use-context` 切走，别在生产集群上操作。**

---

## 阶段 C：抢 ODCR（用本仓库脚本，验"能真抢到"）

```bash
cd /path/to/ec2-i4i-capacity-grabber

# C1. 先演练（不花钱）
python3 grab_odcr.py --azs $AZ1 $AZ2 --per-az-cores 64

# C2. 实弹：每 AZ 1 台 i4i.16xlarge（64 核），共 128 核（⚠️ 开始计费）
python3 grab_odcr.py --azs $AZ1 $AZ2 --per-az-cores 64 --live

# C3. 确认抢到（验证点①）
python3 grab_odcr.py --list
# 期望 summary：us-east-1b 64 vCPU(1台) / us-east-1d 64 vCPU(1台) / TOTAL 128
```

抢不到（现货不足）就加 `--watch --interval 30` 死等；本测试规模小，通常秒抢到。

---

## 阶段 D：self-managed node group 接预留（每 AZ 一个 ASG）

这是验证核心：**自建 ASG（不是 eksctl 托管组）+ `capacity-reservations-first` → 节点落进预留并入集群**。

需要先准备：EKS 优化版 AMI、节点 IAM 实例配置、bootstrap userdata 把节点接入 `i4i-smoke-eks`。

```bash
# D1. EKS 优化 AMI（x86_64，对应集群 K8s 版本，这里以 1.30 为例，按实际改）
AMI=$(aws ssm get-parameter \
  --name /aws/service/eks/optimized-ami/1.30/amazon-linux-2023/x86_64/standard/recommended/image_id \
  --query 'Parameter.Value' --output text)

# D2. 节点安全组 / 角色：直接复用 eksctl 给集群建好的
#     （eksctl 已建好节点可用的 SG 和 instance role；从集群描述里取）
NODE_SG=$(aws eks describe-cluster --name $CLUSTER \
  --query 'cluster.resourcesVpcConfig.clusterSecurityGroupId' --output text)
# 节点 instance profile：用 eksctl 建的，或单独建一个带
# AmazonEKSWorkerNodePolicy / AmazonEKS_CNI_Policy / AmazonEC2ContainerRegistryReadOnly
# + AmazonSSMManagedInstanceCore 的 role，名字带 $TAG。（细节见 eksctl 输出 / IAM）

# D3. 启动模板：只锁 i4i.16xlarge、x86_64 AMI、bootstrap 接入 i4i-smoke-eks
#     userdata 关键是 /etc/eks/bootstrap.sh $CLUSTER
USERDATA=$(printf '#!/bin/bash\n/etc/eks/bootstrap.sh %s\n' "$CLUSTER" | base64 -w0)
LT=$(aws ec2 create-launch-template \
  --launch-template-name ${TAG}-lt \
  --tag-specifications "ResourceType=launch-template,Tags=[{Key=purpose,Value=$TAG}]" \
  --launch-template-data "{
    \"ImageId\":\"$AMI\",
    \"InstanceType\":\"i4i.16xlarge\",
    \"UserData\":\"$USERDATA\",
    \"TagSpecifications\":[{\"ResourceType\":\"instance\",\"Tags\":[
       {\"Key\":\"purpose\",\"Value\":\"$TAG\"},
       {\"Key\":\"kubernetes.io/cluster/$CLUSTER\",\"Value\":\"owned\"}
    ]}]
  }" \
  --query 'LaunchTemplate.LaunchTemplateId' --output text)

# D4. 每 AZ 一个 ASG，capacity-reservations-first，各 desired=1
for PAIR in "$AZ1:$SUB1" "$AZ2:$SUB2"; do
  AZ=${PAIR%%:*}; SUB=${PAIR##*:}
  aws autoscaling create-auto-scaling-group \
    --auto-scaling-group-name ${TAG}-asg-$AZ \
    --launch-template "LaunchTemplateId=$LT,Version=\$Latest" \
    --min-size 1 --max-size 1 --desired-capacity 1 \
    --vpc-zone-identifier "$SUB" \
    --capacity-reservation-specification "CapacityReservationPreference=capacity-reservations-first" \
    --tags "Key=purpose,Value=$TAG,PropagateAtLaunch=true" \
            "Key=kubernetes.io/cluster/$CLUSTER,Value=owned,PropagateAtLaunch=true"
done

# D5. 让节点能加入集群：把节点 instance role ARN 加进 aws-auth（self-managed 必做）
#     <NODE_ROLE_ARN> = D2 里那个节点 role 的 ARN
eksctl create iamidentitymapping --cluster $CLUSTER --region $AWS_REGION \
  --arn <NODE_ROLE_ARN> --group system:bootstrappers --group system:nodes \
  --username system:node:{{EC2PrivateDNSName}}
```

---

## 阶段 E：核实「成功」（端到端取证）

```bash
# E1. 节点入集群且 Ready（验证点②）——等 2~3 分钟
kubectl get nodes -o wide
# 期望：2 个 i4i.16xlarge 节点，STATUS=Ready，分别在 1b / 1d

# E2. 实例落进了我们抢的预留（验证点③）
aws ec2 describe-instances --region $AWS_REGION \
  --filters "Name=tag:purpose,Values=$TAG" "Name=instance-state-name,Values=running" \
  --query 'Reservations[].Instances[].[InstanceId,Placement.AvailabilityZone,CapacityReservationId]' \
  --output table
# 期望：每台都有 CapacityReservationId，且 = 阶段 C 抢到的预留 ID

# E3. 预留可用槽位被占满（1→0）
python3 grab_odcr.py --list      # 每个 AZ 的预留 Available 应为 0

# E4. Pod 真能调度上去（验证点④）
kubectl run smoke-nginx --image=public.ecr.aws/nginx/nginx:latest
kubectl wait --for=condition=Ready pod/smoke-nginx --timeout=120s
kubectl get pod smoke-nginx -o wide   # Running，落在某个 i4i 节点上
```

**全绿 = 成功**：ODCR 抢到 ✅ + self-managed 节点 Ready 入集群 ✅ + 落进预留 ✅ + Pod Running ✅。

---

## 阶段 F：拆除（按 tag 精确删，停止计费）

> 顺序：先删 ASG（连带终止实例）→ 取消 ODCR（止血）→ 删 EKS 集群 → 删网络。
> **每条命令执行前确认名字带 `i4i-smoke-eks` / tag 是 `i4i-smoke-eks`。**

```bash
# F1. 删两个 ASG（force 连带终止实例）
for AZ in $AZ1 $AZ2; do
  aws autoscaling delete-auto-scaling-group \
    --auto-scaling-group-name ${TAG}-asg-$AZ --force-delete
done
sleep 90                                  # 等实例终止、ENI 释放

# F2. 取消 ODCR（停止按台计费 —— 最关键的止血；只取消脚本 tag 的预留）
python3 grab_odcr.py --cancel-all --live
python3 grab_odcr.py --list               # 确认预留清零

# F3. 删启动模板
aws ec2 delete-launch-template --launch-template-id $LT

# F4. 删 EKS 集群（eksctl 连带删它建的 nodegroup/SG/role/cfn 栈）
eksctl delete cluster --name $CLUSTER --region $AWS_REGION    # 再次确认是 i4i-smoke-eks

# F5. 删网络（子网→路由→IGW→VPC）
aws ec2 delete-subnet --subnet-id $SUB1
aws ec2 delete-subnet --subnet-id $SUB2
aws ec2 delete-route-table --route-table-id $RT
aws ec2 detach-internet-gateway --internet-gateway-id $IGW --vpc-id $VPC
aws ec2 delete-internet-gateway --internet-gateway-id $IGW
aws ec2 delete-vpc --vpc-id $VPC

# F6. 零残留扫描（按 tag）
aws ec2 describe-instances --filters "Name=tag:purpose,Values=$TAG" \
  "Name=instance-state-name,Values=pending,running,stopping,stopped" \
  --query 'Reservations[].Instances[].InstanceId' --output text     # 应为空
python3 grab_odcr.py --list                                          # 应无预留
aws eks list-clusters --query "clusters[?@=='$CLUSTER']" --output text  # 应为空
```

---

## 验收清单（这次的 goal）

- [ ] 所有 unit test 通过：`python3 -m unittest test_common test_grab_odcr`（57/57）
- [ ] ODCR 抢到：`--list` 显示两 AZ 各 64 vCPU，active
- [ ] self-managed 节点 Ready：`kubectl get nodes` 两个 i4i 节点 Ready
- [ ] 落进预留：实例 `CapacityReservationId` 命中，预留 Available 1→0
- [ ] Pod 调度成功：`smoke-nginx` Running
- [ ] 全程未触碰 `litellm-cluster`：所有操作的集群名/上下文均为 `i4i-smoke-eks`
- [ ] 零残留：拆除后按 tag 扫描全空，无持续计费

> **注意**：本 runbook 的 EKS/ASG 命令**未在真 AWS 上跑过**（无凭证），是按 AWS API 正确用法编写的标准流程，细节（AMI 版本号、节点 IAM role ARN、aws-auth 映射）需按实际环境填。`grab_odcr.py` 部分的逻辑已被 57 个 unit test 覆盖。建议先在测试账号走一遍，确认无误再正式记录结果。
