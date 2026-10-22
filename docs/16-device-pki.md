# 16 · 设备证书体系（ROOT → CA → Device）

> **本阶段范围**：只动 admin（FastAPI）后台。
> **不**改 hbbs/hbbr，**不**改客户端，也不改现有登录/打洞流程。
> 这一版先把「签发、续期、吊销、mTLS 业务面」的**服务端路由**做实，
> 等客户端下一轮升级再接进去。

---

## 一、要解决的问题

后续 P2P 端到端加密要求每台设备/每个 App 都有一张**服务端 CA 签发**的证书：

- 设备端本地生成密钥对（私钥永不出设备），把 **CSR**（公钥 + 身份声明）发给后台；
- 后台用自己的中间 CA 私钥签发**叶子证书**；
- 客户端内置 ROOT 证书，用 `ROOT → CA → Device` 三级链校验对端证书；
- 会话密钥协商时用证书里的公钥做签名验证，从而替代「只靠服务器下发公钥」的模型。

---

## 二、证书层级

```
ROOT（自签，20 年，生产环境应离线保存）
  └── CA（中间 CA "WeDesk Device CA"，10 年，path_length=0，由 ROOT 签发）
        ├── 叶子：device  （mTLS clientAuth，默认 90 天）
        └── 叶子：app     （mTLS clientAuth，默认 180 天）
```

| 项 | 默认 | 环境变量 |
|---|---|---|
| ROOT 主体 | `WeDesk Root CA` | `PKI_ROOT_CN` |
| CA 主体 | `WeDesk Device CA` | `PKI_CA_CN` |
| 密钥类型 | `ec256`（ROOT/CA 用，叶子密钥由设备自选） | `PKI_KEY_TYPE=ed25519\|ec256\|ec384\|rsa2048\|rsa4096` |
| ROOT / CA 有效期 | 7300 / 3650 天 | `PKI_ROOT_DAYS` `PKI_CA_DAYS` |
| 设备 / App 证书有效期 | 90 / 180 天 | `PKI_DEVICE_CERT_DAYS` `PKI_APP_CERT_DAYS` |
| 续期窗口 | 30 天（只影响 `renew_after` 建议值） | `PKI_RENEW_WINDOW_DAYS` |
| 密钥落盘目录 | `/data/pki` | `PKI_DIR` |
| CRL 分发点 | 不写入证书 | `PKI_CRL_URL` |

文件布局（`PKI_DIR`）：

```
root.key / root.crt   ROOT CA
ca.key   / ca.crt     中间 CA（签所有叶子）
ca_chain.crt          ca.crt + root.crt —— 客户端要信任/安装的就这俩（派生文件）
.wedesk-pki-origin.json  记录哪些文件是本服务生成的（来源标记，派生文件）
```

### 2.1 使用自己的 ROOT / CA（生产必读）

**服务不会覆盖任何已存在的证书文件。** 目录里有就用你的，目录里没有才可能
生成——而且只在 `PKI_AUTO_CREATE=1`（默认）时才生成。判断顺序是：
证书文件存在 → 直接采用；只有空槽位才填。

放进去即可（四种文件都是 PEM）：

```
PKI_DIR/root.crt          你的 ROOT 证书（root.key 可以没有，离线 ROOT 正是推荐形态）
PKI_DIR/ca.crt + ca.key   你的中间 CA —— 必须带私钥，否则签不了叶子
```

也支持用绝对路径精确指定：

| 环境变量 | 含义 |
|---|---|
| `PKI_ROOT_CERT_FILE` / `PKI_ROOT_KEY_FILE` | ROOT 证书 / 私钥路径 |
| `PKI_CA_CERT_FILE` / `PKI_CA_KEY_FILE` | 中间 CA 证书 / 私钥路径 |
| `PKI_KEY_PASSWORD` | 私钥口令（若导出时加密） |
| `PKI_AUTO_CREATE` | `0` = 缺材料就报错，**绝不自己造**（生产推荐） |
| `PKI_ALLOW_UNCHAINED_CA` | `1` = 允许 CA 与 ROOT 不成链（默认拒绝） |

**兼容的文件名**：`root.crt` / `root.pem` / `root.cert.pem`、`ca.crt` / `ca.pem`；
私钥 `*.key` / `*.key.pem`；也接受**私钥和证书写在同一个 PEM 文件里**
（很多 CA 就这么发）。

**离线 ROOT 为什么能工作**：ROOT 私钥只在"签发新的中间 CA"时才需要。只要你
同时提供了 `ca.crt` + `ca.key`，ROOT 有没有私钥都不影响签发设备证书——
所以 `root.key` 可以不进容器，这是推荐做法。

**会直接启动失败的四种情况**（都是宁可停机也不毁证书）：

1. 显式配置的路径不存在（不会退回自动生成，否则等于偷偷换锚点）；
2. `PKI_AUTO_CREATE=0` 且材料缺失；
3. 私钥与证书不匹配 —— **两个文件都原样保留**，报错让你自己看；
4. 提供的 CA 不是由提供的 ROOT 签发的（真签名验证，不是比字符串）；
   或 CA 不是 CA（`CA=FALSE` / 缺 `keyCertSign`）/ 已过期。

**确认用的是不是你自己的证书**：

```bash
curl https://admin.example.com/api/cert/info    # 带管理员会话
curl https://admin.example.com/api/cert/ca      # 公开
```

返回里有 `root_source` / `ca_source`：`provided` = 用的你的文件，
`generated` = 这套是本服务自己造的（首次启动的 dev 便利），此时服务启动日志
会打一行 `[pki] WARNING: using generated CA material`。来源靠
`.wedesk-pki-origin.json` 记录序列号，重启后依然如实上报——不会出现"第一次
自动生成、重启后伪装成 provided"。

---

## 三、接口清单

| 接口 | 路径 | TLS 模式 | 用途 |
|---|---|---|---|
| 硬件激活 | `POST /api/device/activate` | 单向 HTTPS | 硬件首次上线申请证书 |
| App 激活 | `POST /api/app/activate` | 单向 HTTPS | iOS App 首次上线申请证书 |
| 硬件业务 | `/api/device/*` | mTLS | 硬件上报、拉取指令 |
| App 业务 | `/api/app/*` | mTLS | App 控制硬件、查询状态 |
| 证书续期 | `POST /api/cert/renew` | mTLS | 叶子证书到期前静默续证 |

配套（不属于上表五类，但必须存在）：

| 接口 | TLS 模式 | 用途 |
|---|---|---|
| `GET /api/cert/ca` | 单向 | 公开信任锚：ROOT + 中间 CA + chain |
| `GET /api/cert/crl` | 单向 | 吊销列表（PEM） |
| `POST /api/cert/nonce` | 单向 | 可选：服务器下发 nonce |
| `GET /api/cert/status` | mTLS | 当前证书什么时候到期、该不该续 |
| `POST /api/cert/activation-tokens` | 管理员会话 | 铸造一次性激活 token |
| `GET /api/cert/certs`、`GET /api/cert/certs/{id}` | 管理员会话 | 查询已签发证书 |
| `POST /api/cert/revoke` | 管理员会话 | 按 serial 或 device_id 吊销 |
| `GET /api/cert/info` | 管理员会话 | CA 状态自检 |

### 3.1 硬件激活（单向 HTTPS）

```
POST /api/device/activate
{
  "device_id": "123456789012",
  "csr":        "-----BEGIN CERTIFICATE REQUEST-----...",
  "token":      "<一次性激活 token>",
  "timestamp":  "1729...",          // PoP
  "nonce":      "<16 字节 hex>",     // PoP
  "signature":  "<base64>"          // PoP
}
```

成功：

```json
{
  "result": "OK",
  "certificate": "-----BEGIN CERTIFICATE-----...",
  "ca_chain": "-----BEGIN CERTIFICATE-----\n...",   // CA + ROOT
  "root":     "-----BEGIN CERTIFICATE-----...",     // 只含 ROOT，便于客户端固定
  "serial": "44f3c8c7...",
  "fingerprint": "<SPKI 的 SHA-256 hex>",
  "not_before": "...", "not_after": "...", "renew_after": "..."
}
```

失败码：`400` CSR 非法 / `401` token 无效过期或 PoP 不过 / `403` token 与身份不匹配 /
`409` token 已用，或该设备**已持有有效证书**（需吊销后重签，或用 `allow_reissue` 的 token）。

### 3.2 App 激活

与 3.1 完全同构，`cert_type` 为 `app`，路径 `/api/app/activate`。

### 3.3 静默续期（mTLS）

```
POST /api/cert/renew     ← 用【当前】证书做 mTLS
{ "csr": "-----BEGIN CERTIFICATE REQUEST-----..." }
```

- 身份取自**当前客户端证书**，请求体里的 `device_id` **被忽略**——续期不可能越权扩权；
- 新旧密钥可以不同（支持密钥轮换），新旧 serial 通过
  `renewed_from` / `superseded_by` 串起来，`renew_count` 递增；
- 旧证书标记 `superseded`（**在它自己的 `not_after` 之前仍然可用**），
  给客户端留出"换文件"的缓冲；想立刻作废就走吊销。

### 3.4 业务面（mTLS）

| 路径 | 证书类型 | 说明 |
|---|---|---|
| `POST /api/device/heartbeat` | device | 上报存活 + 连接数，回带 `should_renew` |
| `POST /api/device/report` | device | 任意 JSON 遥测上报 |
| `GET /api/device/commands` | device | 拉取 App 排队的指令（取出即置 `sent`） |
| `POST /api/device/command-result` | device | 回报指令结果 |
| `GET /api/app/devices` | app | 设备列表 + 在线状态 |
| `GET /api/app/device/{id}` | app | 单设备状态 + 其证书列表 |
| `POST /api/app/control` | app | 给某设备排一条指令 |

新增业务接口只需一行：

```python
@router.post("/api/device/xxx")
def xxx(identity: mtls.LeafIdentity = Depends(mtls.require_leaf("device")), ...):
    ...
```

---

## 四、mTLS 身份是怎么认定的（重点）

FastAPI/uvicorn **不会**把对端证书放进 ASGI scope，生产上 TLS 又通常由
nginx/Caddy 终结、只把证书以 HTTP 头转发过来。**直接信任这个头等于任何人
都能伪造身份**。所以 `app/mtls.py` 用三重独立校验，头只当作「传输线索」：

1. **链校验**：证书确实由我们的中间 CA 签名（`ca.verify_leaf()` 是真签名验证，
   不是字符串比对）；
2. **库校验**：`serial` **且** `spki_fingerprint` 必须同时命中
   `admin_device_certs` 里 `status="active"` 且未过期的记录
   —— 吊销/过期由**服务端**判定，不依赖反向代理；
3. **持有证明（PoP）**：请求必须带用叶子私钥做的签名。

第 3 条正是「转发头可信」的原因：伪造头的人做不出这个签名。

### 4.1 PoP 规范

请求头：

```
X-WeDesk-Timestamp: <unix 秒>
X-WeDesk-Nonce:     <≥8 字符，一次性>
X-WeDesk-Signature: <base64 或 hex>
```

待签串（`\n` 连接）：

```
METHOD\nPATH\nTIMESTAMP\nNONCE\nsha256_hex(body)
```

- 算法：Ed25519 裸签名；EC → `ECDSA-SHA256`；RSA → 先试 `PSS-SHA256`，回退 `PKCS1v15-SHA256`；
- 时间窗 ±`PKI_POP_WINDOW_SECONDS`（默认 300 秒）；
- nonce 一次性：服务端 `/api/cert/nonce` 下发或客户端自造均可，
  用过即标记，重放返回 `401 nonce replayed`；
- 激活接口是单向 HTTPS、签名放在 JSON 体里（签名不能覆盖自身），
  所以待签串改为 `device_id\nsha256_hex(csr)\nTIMESTAMP\nNONCE`。

### 4.2 nginx 侧必须做的事

```nginx
ssl_client_certificate /data/pki/ca_chain.crt;   # 只信任我们自己的 CA
ssl_verify_client      on;

proxy_set_header X-SSL-Client-Cert      $ssl_client_escaped_cert;
proxy_set_header X-SSL-Client-Verify    $ssl_client_verify;
proxy_set_header X-SSL-Client-Serial    $ssl_client_serial;
proxy_set_header X-SSL-Client-DN        $ssl_client_s_dn;
```

反向代理**必须**挡住这几个头的外来值，否则外部请求可以自己带上
`X-SSL-Client-Cert`（虽然过不了 PoP，但会污染日志和排错）。头名可用
`PKI_MTLS_HEADER_*` 覆盖。想整体关掉 PoP（不推荐）设 `PKI_REQUIRE_POP=0`。

---

## 五、数据表

| 表 | 作用 |
|---|---|
| `admin_device_certs` | 叶子证书台账：serial(hex, PK)、device_id、cert_type、SPKI 指纹、PEM、状态、有效期、`renewed_from`/`superseded_by`/`renew_count`、吊销信息、最后活跃 |
| `admin_activation_tokens` | 一次性激活 token，绑定 device_id + cert_type，用完即焚 |
| `admin_cert_nonces` | PoP 防重放 |
| `admin_device_commands` | App → 设备的指令队列 |

状态机：`active → superseded`（被续期顶替）/ `active → revoked`（吊销）/
`active → expired`（过期后首次访问时自动标记）。

---

## 六、部署

新增依赖：

```
cryptography==50.0.2      # admin/requirements.txt
```

环境变量（完整列表见 `admin/app/config.py` 的 PKI 段）。最小改动只需：

```bash
PKI_DIR=/data/pki                 # 需要持久化，否则每次重启都换 CA
PKI_CRL_URL=https://admin.example.com/api/cert/crl   # 可选，写进证书的 CDP
```

生产环境再加这两条（用你自己的 ROOT/CA，且不允许自动生成）：

```bash
PKI_AUTO_CREATE=0                          # 缺证书就停机报错，绝不自己造一套
PKI_CA_KEY_FILE=/run/secrets/ca.key        # CA 私钥走 secret 挂载，不进镜像
PKI_ROOT_CERT_FILE=/etc/wedesk/root.crt    # ROOT 只要证书（离线 ROOT）
PKI_CA_CERT_FILE=/etc/wedesk/ca.crt
```

`docker-compose` 里给 admin 服务挂卷：

```yaml
volumes:
  - ./data/pki:/data/pki          # 或只读挂载你准备好的 ROOT/CA 目录
```

> 挂载前先确认目录里**已经**有你的 `root.crt` 和 `ca.crt`+`ca.key`。若先空着
> 启动一次，服务会自己生成一套并写入；之后你再放自己的文件也来得及（不会被
> 覆盖），但**已经签发出去的设备证书是用旧 CA 签的，需要重新激活**。

启动后自检：

```bash
curl https://admin.example.com/health      # pki: true
curl https://admin.example.com/api/cert/ca # root_source/ca_source 应为 provided
```

---

## 七、客户端后续怎么接（下一轮）

1. 出厂/首次启动时本地生成密钥对（wedesk 现网已用 ed25519，可直接复用）；
2. 构造 CSR（CN = 设备 ID），连同一次性激活 token 调 `/api/device/activate`；
3. 把返回的 `ca_chain` 与内置 ROOT 交叉验证后再落盘，**只信内置 ROOT**；
4. 存下 `not_after`，`now > renew_after` 时静默调 `/api/cert/renew`；
5. 业务请求统一带 PoP 三头；
6. 内置 ROOT 建议做**证书固定**（pinning）：ROOT 20 年不换，
   中途换 ROOT 必须走客户端升级。

> 注意：本阶段**没有**改动客户端，所以现在所有 `/api/device/*`、`/api/app/*`
> 只有实现了上述流程的客户端/测试脚本能调通，老客户端完全不受影响。

---

## 八、验证

```bash
cd admin
pip install -r requirements.txt
python tests/test_cert_api.py        # 端到端：激活 / 续期 / mTLS / 吊销 / 伪造
python tests/test_pki_external.py    # 自带 ROOT/CA 的覆盖防护（10 组场景）
```

`test_cert_api.py` 覆盖 15 组、30 项断言：CA 引导 → 激活（ed25519 / EC 两种密钥）
→ token 单次性 → mTLS 业务调用 → **重放拒绝** → 证书类型越权拒绝 → 密钥轮换续期 →
旧证书被顶替后拒绝 → 吊销即时生效并进 CRL → 伪造 CA 拒绝 → 无证书拒绝。

`test_pki_external.py` 覆盖自带材料的各种摆放方式与故障模式：离线 ROOT（只有
证书）+ 自带 CA 原样采用且字节不被改写、二次启动幂等、`PKI_AUTO_CREATE=0` 缺
材料即报错、离线 ROOT 缺 CA 时不乱签、密钥与证书不匹配时**双方文件都保留**、
显式路径不存在不退回自动生成、CA 与 ROOT 不成链默认拒绝、合并 PEM 识别、
空目录仍能自举、非 CA 证书不能冒充中间 CA、来源标记不被冒认。

---

## 九、已知限制

| 限制 | 说明 |
|---|---|
| ROOT 可能自动生成 | 目录为空且 `PKI_AUTO_CREATE=1` 时会自己造一套；生产设 `PKI_AUTO_CREATE=0` 并挂离线 ROOT，`root.key` 别进镜像 |
| 续期不强制窗口 | `renew_after` 只是建议值，早续期也允许（否则设备时钟漂移会锁死自己） |
| 吊销靠服务端表 + CRL | 没有 OCSP；客户端必须自己拉 `/api/cert/crl` 或依赖服务端库校验 |
| uvicorn 直连拿不到对端证书 | 必须走反向代理转发头（见 4.2），或改由支持 scope 的服务器托管 |
| 老客户端无证书 | 不受影响，本套接口是**新增**面，与 `/api/heartbeat` 等旧接口并存 |
