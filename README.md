# Synamedia vDCM Zabbix 監控模板

Synamedia vDCM 編碼器的 Zabbix 監控套件，包含 HTTP API 主動查詢、SNMP Trap 分級告警、Bitrate LLD 自動發現與設備風暴壓制。

---

## 目錄

- [檔案結構](#檔案結構)
- [部署架構總覽](#部署架構總覽)
- [快速部署](#快速部署)
- [SNMP Trap 告警設計](#snmp-trap-告警設計)
- [Macros 參數表](#macros-參數表)
- [Host 端 SNMP Trap 前置設定](#host-端-snmp-trap-前置設定)
- [IP-Map 自動同步腳本](#ip-map-自動同步腳本)
- [常見維運操作](#常見維運操作)
- [故障排除](#故障排除)
- [附錄 A：完整 fault type 分級表](#附錄-a完整-fault-type-分級表)
- [附錄 B：主要 Zabbix 物件清單](#附錄-b主要-zabbix-物件清單)

---

## 檔案結構

```
Synamedia/
├── README.md                                                   # 本手冊
├── Synamedia vDCM Dashboard.yaml                               # Zabbix Dashboard 匯入檔
├── Synamedia vDCM Encoder by HTTP API (FTV).yaml               # FTV IDC 主機模板
├── Synamedia vDCM Encoder by HTTP API (SPORTCAST IDC).yaml     # SPORTCAST IDC 主機模板
├── Synamedia vDCM Encoder by HTTP API (YU-WEI IDC).yaml        # YU-WEI IDC 主機模板（主力）
├── vdcm_update_ip_map.py                                       # 自動同步 {$SNMP.IP.MAP} 巨集
└── .venv/                                                      # Python 執行環境
```

模板檔案內容彼此獨立，本文件以 **YU-WEI IDC** 版本為說明主體。其他 IDC 版本結構相同、fault 分級同步。

---

## 部署架構總覽

```
┌────────────────────────────────────────────────────────────────┐
│                    Zabbix Server (172.28.200.53)               │
│                                                                │
│  ┌──────────────┐   ┌────────────────────────────────────┐    │
│  │ SNMP Trapper │   │ HTTP API Collector (vdcm.data)     │    │
│  │  (port 162)  │   │  30 秒一次，一次登入拉齊所有數據    │    │
│  └──────┬───────┘   └──────────────┬─────────────────────┘    │
│         │                          │                           │
│         ▼                          ▼                           │
│  snmptrap[trapControlMsgTrap]   vdcm.data (JSON blob)         │
│    (master, LOG type)              │                           │
│         │                          ├─ vdcm.alarms.detail      │
│         │                          ├─ vdcm.input.bitrates     │
│         │                          ├─ vdcm.port5.bitrates     │
│         │                          ├─ vdcm.services.bitrates  │
│         │                          ├─ vdcm.snmp.ip.map        │
│         │                          └─ vdcm.device.status      │
│         │                                                      │
│         ├─ snmptrap.p1[CH|FAULT]  ← LLD P1 (High)             │
│         ├─ snmptrap.p2[CH|FAULT]  ← LLD P2 (Average)          │
│         ├─ snmptrap.p3[CH|FAULT]  ← LLD P3 (Warning)          │
│         └─ Device Storm Trigger (Disaster)                    │
└────────────────────────────────────────────────────────────────┘
                            │
                            ▼
              ┌────────────────────────────┐
              │  vDCM Devices              │
              │  - litv_tran1 (208:4431)   │
              │  - litv_tran2 (210:4431)   │
              │  SNMP trap sender (.115)   │
              └────────────────────────────┘
```

**兩條資料路徑並行：**
1. **HTTP API pull**：每 30 秒登入 vDCM Web UI，撈 bitrate、alarm、service tree、channel 名稱等。
2. **SNMP trap push**：vDCM 主動發 trap 到 Zabbix Server 162 port，經 parser 分派給各級 (channel × fault) item。

---

## 快速部署

### 前置需求

- Zabbix 7.4+ Server（本專案基於 7.4.11 驗證）
- Zabbix Server 具備 SNMP Trapper 常駐（設定見下方 Host 端章節）
- 可從 Zabbix Server 存取每台 vDCM 的 Web UI（`https://<ip>:4431`）

### 匯入模板

**方式 A：Web UI 匯入**
1. Zabbix Web → Data collection → Templates → Import
2. 選擇 `Synamedia vDCM Encoder by HTTP API (YU-WEI IDC).yaml`
3. 勾選 Create new + Update existing（Discovery rules 不要勾 Delete missing）
4. Import

**方式 B：API 匯入（自動化）**
```bash
python3 - <<'PY'
import json,ssl,urllib.request
URL='https://zabbix-01.svc.litv.tv/api_jsonrpc.php'
ctx=ssl.create_default_context(); ctx.check_hostname=False; ctx.verify_mode=ssl.CERT_NONE
def rpc(m,p,a=None):
    d={"jsonrpc":"2.0","method":m,"params":p,"id":1}
    h={"Content-Type":"application/json"}
    if a: h["Authorization"]=f"Bearer {a}"
    return json.load(urllib.request.urlopen(
        urllib.request.Request(URL,data=json.dumps(d).encode(),headers=h),
        context=ctx,timeout=60))['result']
tok=rpc("user.login",{"username":"Admin","password":"<PASSWORD>"})
with open('Synamedia vDCM Encoder by HTTP API (YU-WEI IDC).yaml') as f: src=f.read()
print(rpc("configuration.import",{"format":"yaml","source":src,"rules":{
    "templates":{"createMissing":True,"updateExisting":True},
    "items":{"createMissing":True,"updateExisting":True,"deleteMissing":False},
    "triggers":{"createMissing":True,"updateExisting":True,"deleteMissing":False},
    "discoveryRules":{"createMissing":True,"updateExisting":True,"deleteMissing":False},
    "template_groups":{"createMissing":True,"updateExisting":True},
}},tok))
PY
```

### 連結模板到 Host + 設定 Macros

每台 vDCM 主機（如 `litv_tran1`、`litv_tran2`）：

1. Host → Templates → Link
2. Host → Macros → 至少設定：

| Macro | 值 | 用途 |
|---|---|---|
| `{$VDCM.URL}` | `https://211.21.3.208:4431` | vDCM Web UI 位址 |
| `{$VDCM.USER}` | `Administrator` | 登入帳號 |
| `{$VDCM.PASS}` | `<secret>` | 登入密碼（設為 Secret 類型） |

3. Host → Interfaces → 新增 **SNMP interface**（IP 隨意設，port 161）— 這是 Zabbix trap 對應 host 的必要條件。

---

## SNMP Trap 告警設計

### 分級策略（依據 Zabbix 標準嚴重度）

三個層級，覆蓋 38 種 Synamedia fault type：

| 層級 | 觸發條件 | Zabbix Severity | 適用 fault 類型 |
|---|---|---|---|
| **P1 立即** | 5 分鐘內同一 (頻道, 故障) ≥ 5 次 | Disaster (5) | 服務中斷級（Service Loss, Video Input Loss …）— 頻道真的斷訊 |
| **P2 累積** | 15 分鐘內同一 (頻道, 故障) ≥ 20 次 | Average (3) | 持續故障（TS Out Loss, PAT Error, PCR 遺失 …） |
| **P3 噪音** | 60 分鐘內同一 (頻道, 故障) ≥ 300 次 | Warning (2) | 常態噪音（PTS Discontinuity, CC Error …） |

各層級的**恢復條件**：
- 收到對應的 `— Cleared` trap → **立即** recover
- **或**該 (頻道, 故障) 靜默 `{$SNMP.TRAP.SILENCE}`（預設 60 分鐘）→ recover（保險機制）

> **設計原則**：`Disaster = 頻道真的斷訊`。個別 P1 故障就是最高等級告警，不再依賴任何「風暴壓制」概念。

### LLD 與 Trigger 命名

**LLD rules（每個層級一支）**
- `snmp.channel.p1.discovery` — 依 `{$SNMP.IP.MAP}` 現有頻道 × P1 fault 清單
- `snmp.channel.p2.discovery`
- `snmp.channel.p3.discovery`

**Item key 格式**
```
snmptrap.p1[<channel>|<fault>]
snmptrap.p2[<channel>|<fault>]
snmptrap.p3[<channel>|<fault>]
```

**Trigger 命名**：`{#CH}: {#FAULT}`  
例：`CTVMainQT: PTS Discontinuity`、`FTV_CTS: Bandwidth Exceeded`

### Trigger Dependencies

無。三個 tier 的個別 trigger 各自獨立觸發、獨立恢復。P1 為 Disaster 級，直接反映「頻道服務中斷」。

### Trigger Tags

每個 trigger 都帶下列 tag，供通知端（bot）分流、批次、靜音使用：

| Tag | 值範例 | 用途 |
|---|---|---|
| `source` | `vdcm-snmp` | 統一標記告警來源 |
| `tier` | `P1` / `P2` / `P3` / `STORM` | 分級 |
| `severity` | `high` / `average` / `warning` / `disaster` | 嚴重度（重複標籤方便查詢）|
| `channel` | `CTVMainQT` | 頻道名，Bot 可依此做 digest |
| `fault` | `Service loss at output` | 故障類型，可做趨勢分析 |
| `scope` | `device`（僅風暴 trigger） | 設備級告警識別 |

---

## Macros 參數表

### SNMP Trap 分級門檻

| Macro | 預設 | 說明 |
|---|---|---|
| `{$SNMP.TRAP.P1_WINDOW}` | `5m` | P1 累計視窗 |
| `{$SNMP.TRAP.P1_MIN_COUNT}` | `5` | P1 觸發門檻 |
| `{$SNMP.TRAP.P2_WINDOW}` | `15m` | P2 累計視窗 |
| `{$SNMP.TRAP.P2_MIN_COUNT}` | `20` | P2 觸發門檻 |
| `{$SNMP.TRAP.P3_WINDOW}` | `60m` | P3 累計視窗 |
| `{$SNMP.TRAP.P3_MIN_COUNT}` | `300` | P3 觸發門檻 |
| `{$SNMP.TRAP.SILENCE}` | `60m` | 靜默保險 recovery 時間 |

### 頻道對應

| Macro | 預設 | 說明 |
|---|---|---|
| `{$SNMP.IP.MAP}` | `{}` | multicast TS `IP:port → channel name` JSON map；由 `vdcm_update_ip_map.py` 自動同步 |
| `{$INPUT.NAME.MAP}` | `{}` | Input 名稱手動對應表（僅 SID 無法唯一辨識時用）|
| `{$INPUT.PROC.NAME.MAP}` | `{}` | Input → Processing service 手動對應（僅無法自動對應時用）|

### vDCM 連線

| Macro | 預設 | 說明 |
|---|---|---|
| `{$VDCM.URL}` | 空 | `https://<ip>:<port>`（Host 必填）|
| `{$VDCM.USER}` | `Administrator` | Web UI 帳號 |
| `{$VDCM.PASS}` | 空 | Web UI 密碼（Host 必填、建議 Secret）|
| `{$VDCM.INPUT.CARDS}` | `T_I:1:4,T_I:1:65532` | Input card xID |
| `{$VDCM.OUTPUT.CARD}` | `T_O:1:4` | Output Port 5 card xID |
| `{$VDCM.PROC.CARDS}` | `T_I:2:0` | vMFP processing card xID |
| `{$VDCM.PROC.NODE}` | `T_T:2:*:*:e0` | vMFP processing node xID |

### Bitrate 門檻

| Macro | 預設 | 說明 |
|---|---|---|
| `{$INPUT.BITRATE.MIN}` | `500`（Kbps） | Input critical 門檻 |
| `{$INPUT.BITRATE.WARN}` | `2500`（Kbps） | Input warning 門檻 |
| `{$PORT5.BITRATE.MIN}` | `2000`（Kbps） | Port 5 SRT critical 門檻 |
| `{$PORT5.BITRATE.WARN}` | `5000`（Kbps） | Port 5 SRT warning 門檻 |
| `{$VIDEO.BITRATE.MIN}` | `50`（Kbps） | Video critical 門檻 |
| `{$VIDEO.BITRATE.WARN}` | `150`（Kbps） | Video warning 門檻 |

---

## Host 端 SNMP Trap 前置設定

在 Zabbix Server（`172.28.200.53`）需要 SNMP Trapper 常駐並寫入指定檔案，Zabbix Server 才會處理 trap。

### 檢查

```bash
# Zabbix Server 設定
grep -E '^(StartSNMPTrapper|SNMPTrapperFile)' /etc/zabbix/zabbix_server.conf
# 應有：
#   StartSNMPTrapper=1
#   SNMPTrapperFile=/var/log/snmptrap/snmptrap.log

# snmptrapd 常駐
systemctl status snmptrapd

# trap log 有內容
sudo tail -f /var/log/snmptrap/snmptrap.log
```

### Host 對應

- 每台 vDCM host 必須有 **SNMP interface**（type=SNMP），Zabbix 依 SNMP 來源 IP 對應到 host。
- Synamedia 設備可能將 SNMP trap agent-addr 標為 management IP 而非 web IP。目前實際觀察到 trap 由 `211.21.3.115` 發出，`ZBXTRAP` 標籤則指向 `211.21.3.208 / 211.21.3.210`。Zabbix 會依 `ZBXTRAP` 標籤內的 IP 對應 host，因此 SNMP interface IP 應與此值相符。

---

## IP-Map 自動同步腳本

`vdcm_update_ip_map.py` 會定期從各 vDCM host 的 `vdcm.snmp.ip.map` item 讀取最新頻道對應表，寫回該 host 的 `{$SNMP.IP.MAP}` 巨集。

### 已排程於 Zabbix Server

```
/etc/zabbix/scripts/vdcm_update_ip_map.py   # 0700 root:root（含密碼）
/etc/cron.d/vdcm_ip_map                     # 每 5 分鐘執行
/var/log/vdcm_ip_map.log                    # 執行記錄
```

Cron 內容：
```
*/5 * * * * zabbix /usr/bin/python3 /etc/zabbix/scripts/vdcm_update_ip_map.py >> /var/log/vdcm_ip_map.log 2>&1
```

### 手動執行

```bash
sudo -u zabbix /usr/bin/python3 /etc/zabbix/scripts/vdcm_update_ip_map.py
```

輸出範例：
```
[OK]    litv_tran1: {$SNMP.IP.MAP} already up to date (71 entries)
[OK]    litv_tran2: {$SNMP.IP.MAP} already up to date (71 entries)
```

### 腳本內建處理

- 剝除 vDCM UI 回傳的 HTML 標籤（`<span style='...'>` 等）
- 將 `Service0N (Real)` 拆為 `Real`（例：`Service01 (DayStar)` → `DayStar`）
- 只保留 multicast `IP:port` 與 `:port` fallback（trap parser 實際會查的鍵）
- 超過 Zabbix 巨集長度上限（2048 字元）時，自動丟棄重複值 ≥ 3 次的條目直到能塞下

---

## 常見維運操作

### 新增一台 vDCM host

1. Zabbix Web → Hosts → Create
2. Groups 選擇適當群組
3. Interfaces：加一個 SNMP interface，IP = vDCM ZBXTRAP 標籤內的 IP
4. Templates：Link `Synamedia vDCM Encoder by HTTP API - <IDC>`
5. Macros：填 `{$VDCM.URL}`、`{$VDCM.USER}`、`{$VDCM.PASS}`
6. Save → 等 30 秒 → `vdcm.data` 開始收到值
7. 等 5 分鐘 cron 執行 → `{$SNMP.IP.MAP}` 自動填入

### 某頻道太吵想暫時靜音

**選項 A：手動關 Problem，等它自然 recover**
- Zabbix Web → Monitoring → Problems → 勾該 problem → Close problem

**選項 B：把該頻道對應 fault 提高門檻**
- Host → Macros → 新增 override，例：
  - `{$SNMP.TRAP.P3_MIN_COUNT}` = `50`（P3 全體升高）
- 或直接在 Zabbix trigger 上 Disable

**選項 C：短期停該頻道整個 tier**
- Zabbix Web → Data collection → Discovery rules → 找對應 LLD → Disabled

### 值班交接看目前有哪些真實故障

```bash
# 全站當下 open problems
ZBX_URL='https://zabbix-01.svc.litv.tv' ZBX_USER='Admin' ZBX_PASS='<pass>' python3 -c "
import json,os,ssl,urllib.request
URL=os.environ['ZBX_URL']+'/api_jsonrpc.php'
ctx=ssl.create_default_context(); ctx.check_hostname=False; ctx.verify_mode=ssl.CERT_NONE
def rpc(m,p,a=None):
    d={'jsonrpc':'2.0','method':m,'params':p,'id':1}
    h={'Content-Type':'application/json'}
    if a: h['Authorization']=f'Bearer {a}'
    return json.load(urllib.request.urlopen(urllib.request.Request(URL,data=json.dumps(d).encode(),headers=h),context=ctx,timeout=20))['result']
tok=rpc('user.login',{'username':os.environ['ZBX_USER'],'password':os.environ['ZBX_PASS']})
hosts=rpc('host.get',{'output':['hostid','host'],'filter':{'host':['litv_tran1','litv_tran2']}})
probs=rpc('problem.get',{'hostids':[h['hostid'] for h in hosts],'output':'extend','sortfield':['eventid'],'sortorder':'DESC'},tok)
for p in probs: print(f\"sev={p['severity']} {p['name']}\")
"
```

### 想調整分級門檻

在 host（或 template）macros 修改：
- 太吵：把 `_MIN_COUNT` 拉高、`_WINDOW` 拉長
- 太安靜：反向
- 想要短時間試 → 改 host macro，不會動到 template

修改後 **不用重跑 LLD**，Zabbix trigger 會立即用新值評估。

---

## 故障排除

### `snmptrap[trapControlMsgTrap]` item is not supported

- 原因：parser 解析 trap 時錯誤造成 item 進 unsupported 狀態。
- 檢查：Zabbix Web → item → 看 Error 欄。
- 常見原因：巨集 `{$SNMP.IP.MAP}` 內有非法 JSON（自動同步腳本會處理，若手動編輯需注意）。

### `vdcm.data` 一直 unsupported

- 原因：master collector JavaScript 執行失敗。
- 檢查：Zabbix Web → item → Latest data → 看 lastvalue 或 error。
- 常見原因：
  - `{$VDCM.URL}` 錯誤或無法連線
  - `{$VDCM.PASS}` 錯誤
  - JavaScript 語法錯誤（`node --check` 可先本地驗證）

### Trigger 顯示 `Cannot evaluate function ...: item is not supported`

- 原因：depends-on 的 item 不 supported，trigger 無法評估。
- 修法：先讓 item 回到 supported（見上兩點）。

### 收到不屬於這台 host 的 trap（例：tran1 收到 CTSMainQT）

- 原因：Synamedia 設備 SNMP agent-addr 設定錯誤，把別台 host 的 trap 標成這台。
- Zabbix 端已有 parser 保護：只要 fault 對應的 channel 不在該 host 的 `{$SNMP.IP.MAP}`，parser 會回傳空字串、不會產生告警。
- 若還是有漏網之魚，去 Synamedia 端修正 trap 目標與 agent-addr 是根本解法。

### 通知洪水

- 檢查是否有 Storm trigger 在同時 fire（Disaster 級）→ 表示設備級問題，個別 P1/P2/P3 應該被抑制、只發 1 筆 Storm。
- 若 Storm 未 fire 但仍有多筆通知 → 把該 tier 的 `_MIN_COUNT` 拉高。
- Bot 端可以用 tag（`source=vdcm-snmp`、`channel=X`）做 digest。

---

## 附錄 A：完整 fault type 分級表

依實際 trap log 統計，共 38 種 fault type。

### P1 High（10 種，一次即報）

| Fault | 意義 |
|---|---|
| Service Loss | 服務整體遺失 |
| Service loss at output | 輸出端服務遺失 |
| Video Input Loss | 訊源影像遺失 |
| Audio Input Loss | 訊源聲音遺失 |
| TS Loss | Transport Stream 遺失 |
| TS Sync Loss | TS 同步遺失 |
| Packager Output Partitioning Failure | HLS/DASH 分段失敗 |
| Packager Origin Server Not Connected | Origin server 未連線 |
| SRT connection loss | SRT 連線中斷 |
| MFP Processing Overload | 編碼器過載 |

### P2 Average（17 種，5 次 / 5 分鐘）

| Fault | 意義 |
|---|---|
| TS Out Loss | 輸出 TS 遺失 |
| Packager Input Data Loss | 進 packager 資料遺失 |
| Packager Input PCR Not Received In Time | PCR 遲到 |
| Packager Input PTS Not Received In Time | PTS 遲到 |
| Packager Input EBP Not Received | EBP 未收到 |
| Packager Video Sync | 影像同步問題 |
| Packager Input Stream Config Mismatch | 進 packager 串流設定不合 |
| Packager Input Stream Alignment Error | 串流對齊錯誤 |
| Bandwidth Exceeded | 頻寬超限 |
| EBP out of sync | EBP 失同步 |
| SRT Sender Buffer Overflow | SRT 傳送 buffer 滿 |
| Threshold Reached for the SRT Lost Packets | SRT 掉包達門檻 |
| Source IP address of TS changed | TS 來源 IP 變更 |
| Invalid Resolution | 無效解析度 |
| PAT Error | PAT 表錯誤 |
| PMT Error | PMT 表錯誤 |
| PID Error | PID 錯誤 |

### P3 Warning（11 種，20 次 / 15 分鐘）

| Fault | 意義 |
|---|---|
| PTS Discontinuity | PTS 不連續（常態噪音） |
| CC Error | Continuity Counter 錯誤（多播掉包常見） |
| User Selected PCR PID Error | PCR PID 選擇不合 |
| No PCR Present | 無 PCR（通常是啟動瞬時） |
| UDP Stream Loss | UDP 短暫遺失 |
| Dejitter Buffer Reset | Buffer 重置 |
| Audio PTS Too Far Off | 聲音 PTS 偏移 |
| RTP Buffer Too Small For Fixed Delay | RTP buffer 設定過小 |
| Time Source Unavailable | NTP 來源不可用 |
| Licensing Warning | 授權警告 |
| Audio Processing End-to-End Delay Too Low | 聲音端到端延遲過低 |

### 未列入清單的新 fault type

若 Synamedia 韌體更新後出現新 fault type：
1. 個別 (channel, fault) trigger **不會** fire（LLD 沒建對應 item）
2. **設備風暴 trigger 仍會抓到**（因為它只算 master item 內所有非-Cleared trap 數量）
3. 從 Storm 通知順便看 master item 的 log，找出新 fault type，加入本文件對應層級的清單，並更新 template。

---

## 附錄 B：主要 Zabbix 物件清單

### Master items（主資料源）

| Key | 型別 | 用途 |
|---|---|---|
| `snmptrap[trapControlMsgTrap]` | SNMP trap | 所有 vDCM SNMP trap 進入點，含 parser preprocessing |
| `vdcm.data` | Script | 30 秒一次拉齊所有 HTTP API 資料，回傳 JSON blob |

### Dependent items（依附 master）

| Key | 說明 |
|---|---|
| `vdcm.alarms.detail` | 過濾後的 device alarm 文字 |
| `vdcm.device.status` / `.state` / `.vdcm_state` | 設備健康 |
| `vdcm.input.bitrates` | 所有 input stream bitrate（LLD master）|
| `vdcm.port5.bitrates` | Port 5 SRT stream bitrate（LLD master）|
| `vdcm.services.bitrates` | Processing service bitrate（LLD master）|
| `vdcm.snmp.ip.map` | 自動計算的 TS→channel map（IP-map 同步腳本讀這個）|
| `vdcm.output.stopped_count` | 停擺的 output service 數量 |
| `vdcm.proc.bitrate` | Processing 總 bitrate |
| `vdcm.processing.alarms` | Processing 層 fault 文字 |
| `vdcm.packager.alarms` | Packager 層 fault 文字 |

### Discovery rules

| Key | 用途 |
|---|---|
| `snmp.channel.p1.discovery` | SNMP trap P1 tier LLD |
| `snmp.channel.p2.discovery` | SNMP trap P2 tier LLD |
| `snmp.channel.p3.discovery` | SNMP trap P3 tier LLD |
| `vdcm.alarm.discovery` | 逐 channel LLD（配合 alarms_json）|
| `vdcm.input.discovery` | Input stream LLD |
| `vdcm.port5.discovery` | Port 5 SRT stream LLD |
| `vdcm.services.discovery` | Processing service LLD |

### 主要 triggers

| 名稱 | 嚴重度 | 觸發 |
|---|---|---|
| `vDCM device-wide trap storm (>= N alarms / T)` | Disaster | 設備級 trap 洪水 |
| `{#CH}: {#FAULT}`（P1 LLD 產生） | High | 服務中斷級 fault |
| `{#CH}: {#FAULT}`（P2 LLD 產生） | Average | 持續故障 |
| `{#CH}: {#FAULT}`（P3 LLD 產生） | Warning | 常態噪音累積 |
| `vDCM: state is not Active` | High | vDCM 主機狀態異常 |
| `vDCM: no bitrate data for 5+ minutes` | High | HTTP API 拉不到資料 |
| `vDCM: total output bitrate dropped below threshold` | High | 總輸出 bitrate 低於門檻 |
| `vDCM: real alarm text active` | Average | 有非背景噪音的真實 alarm |
| `{#SVCNAME}: vMFP service crashed` | Disaster | 特定 service heartbeat = 0 |
| `{#INNAME}: INPUT stream lost` | Disaster | Input stream bitrate = 0 |
| `{#INNAME}: input bitrate critically low / low` | High / Warning | Input bitrate 過低 |
| `{#STREAM.NAME}: Port 5 SRT stopped` / `low` | Disaster / Warning | Port 5 SRT 異常 |

---

## 變更歷程

- **2026-09-12**：完成告警設計 v2
  - 每 (channel × fault) 獨立 LLD → 分成 P1 / P2 / P3 三層
  - 加入設備風暴壓制（Disaster）
  - 加入 trigger tag 分流機制
  - 擴充覆蓋率至 38 種 fault type

- **2026-09-11**：完成 SNMP trap parser 重構
  - 新增 (channel, fault) LLD 架構
  - `nodata()` 恢復改為 macro 驅動
  - `{$SNMP.IP.MAP}` 自動同步腳本 + cron 排程
