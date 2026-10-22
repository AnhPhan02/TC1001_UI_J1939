# Kế hoạch triển khai đồng bộ J1939 Pattern Generator — Firmware và UI

## 1. Mục tiêu và quyết định kiến trúc

### 1.1 Mục tiêu sản phẩm

Xây dựng J1939 Pattern Generator dùng STM32F103C8T6 và UI Python/PySide6 + TSMaster để:

- quản lý catalog 179 bản ghi SPN thuộc 60 PGN;
- hỗ trợ toàn bộ 57 PGN single-frame, tương ứng 175 bản ghi SPN;
- cho phép tối đa 100 SPN hoạt động đồng thời trên STM32F103C8;
- hỗ trợ ba PGN payload lớn bằng J1939 Transport Protocol;
- cấu hình, chạy, dừng, giám sát và xác minh dữ liệu đồng bộ giữa UI và FW;
- không làm mất frame do burst nội bộ và không tạo tải CAN vượt giới hạn đã phê duyệt.

### 1.2 Các mặc định được khóa cho kế hoạch này

- Bus: Classical CAN, SAE J1939-21, extended 29-bit identifier.
- Bitrate mặc định: 250 kbit/s; 500 kbit/s là tùy chọn kiểm thử.
- Python UI sử dụng TSMaster Native API làm transport owner duy nhất; control và J1939 data dùng chung một CAN channel/hardware mapping.
- Catalog chứa đủ 179 SPN, nhưng giới hạn runtime là 100 active SPN và 60 PGN.
- Giai đoạn đầu chạy trên mạng bench cô lập với source address tĩnh.
- Làm single-frame trước; Transport Protocol triển khai BAM trước, RTS/CTS sau.
- Mọi lịch truyền được quản lý theo PGN, không theo từng SPN.
- `(PGN, SPN)` là identity chuẩn; `signal_key` 1-based là khóa truyền trên control protocol.
- Ba key hiện có `103`, `97`, `144` phải giữ nguyên.
- Các transmission rule chưa rõ không được tự suy diễn; chúng ở trạng thái `UNRESOLVED` và không tự phát.

### 1.3 Ngoài phạm vi giai đoạn đầu

- CAN FD/J1939-22.
- Hỗ trợ tổng quát payload 1785 byte; project chỉ cấp tối đa 200 byte.
- Kết nối trực tiếp vào mạng xe đang vận hành trước khi có Address Claim và đánh giá an toàn bus.
- Cho phép 179 SPN cùng active trên STM32F103C8.

## 2. Baseline và các lỗi phải xử lý

### 2.1 Hiện trạng xác nhận

- FW đã chứa metadata 179 SPN nhưng runtime mặc định chỉ có 8 signal/8 PGN.
- UI mới điều khiển và decode EEC1/SPN 190.
- Control protocol v1 có handshake, status, retry, dedup, SET_CONST, START và STOP.
- FW chỉ gửi frame vật lý tối đa 8 byte; chưa có TP.
- Build hiện tại dùng khoảng 34.9 KB Flash và 4.7 KB RAM.
- 45 PGN periodic theo dataset tạo khoảng 697 frame/s; tải dự kiến tại 250 kbit/s khoảng 36–45% trước traffic khác.

### 2.2 Lỗi bắt buộc sửa trước khi scale

- Tách `control_address = 0x80` khỏi source address của virtual ECU (`0x00`, `0x03`, `0x0B`, ...).
- Không dùng period `0` trong periodic scheduler.
- Bổ sung rollback để việc đăng ký pattern, signal và PGN là atomic.
- Không reset mọi PGN timer cùng một timestamp.
- Không abort cả ba bxCAN mailbox khi một lần gửi bị timeout.
- Không log từng RX frame trực tiếp lên Qt UI ở tải cao.
- Không dùng bảng timing mặc định 1000 ms cho các PGN chưa được khai báo thủ công.
- Khôi phục test environment: UI phải có PySide6; FW host-test phải build/chạy tái lập được.

## 3. Kiến trúc đích và luồng dữ liệu

```text
Workbook chuẩn
      |
      v
Catalog generator + validator
      |------------------------------|
      v                              v
FW generated catalog             UI generated catalog
Flash metadata                   Search/edit/decode metadata
      |                              |
      |<--- protocol version + catalog CRC --->|
      |
Active profile (<=100 SPN)
      v
PGN-centric scheduler
      v
Payload encoder: 8 B hoặc <=200 B
      v
Single frame / J1939 TP
      v
Software TX queue -> 3 bxCAN mailboxes -> CAN bus
      v
TSMaster RX -> UI cache -> UI refresh 5–10 Hz
```

### 3.1 Kết nối Python UI — TSMaster — STM32

```text
PySide6 UI
    |
    v
CanWorker duy nhất trong worker thread
    |
    v
TSMaster Native DLL/API
    |
    v
Một logical CAN channel -> một hardware mapping -> một CAN bus
    |
    +-- Control request: PC SA 0xF9 -> destination 0x80, PGN 0xEF00
    +-- Control response: FW SA 0x80 -> destination 0xF9, PGN 0xEF00
    +-- J1939 data: broadcast từ source address của từng virtual ECU
    +-- J1939 TP: TP.CM/TP.DT trên cùng channel khi payload >8 byte
```

Quy tắc bắt buộc:

- Production chỉ dùng `TSMasterBackend` dựa trên Native API; `MockBackend` chỉ dùng offline/test.
- COM backend không nằm trên production path trừ khi được xác nhận lại bằng hardware test riêng.
- Chỉ CanWorker được gọi TSMaster API; UI thread không trực tiếp send/receive CAN.
- Cùng một connection config được dùng cho control, data và TP: channel, bitrate và hardware mapping phải thống nhất.
- Một thời điểm chỉ có một process sở hữu TSMaster application/hardware session. Không chạy đồng thời TSMaster Desktop measurement và Python UI nếu driver/API không cho phép chia sẻ.
- UI phân loại frame theo PGN, DA và SA sau khi đọc từ cùng RX FIFO; không tạo CAN connection riêng cho control và readback.
- Control ACK/timeout không được xử lý bằng cách chặn RX data; worker phải tiếp tục drain FIFO trong khi chờ ACK.
- Raw trace có thể xem/lưu qua TSMaster, nhưng việc render log trong PySide6 phải rate-limit để không làm chậm RX FIFO.

### 3.2 Một nguồn dữ liệu chung

Workbook master trong FW là nguồn chuẩn. Một generator duy nhất phải tạo:

- C catalog cho FW;
- JSON hoặc Python catalog cho UI;
- manifest chứa schema version, catalog CRC32, 179 signal count, 60 PGN count;
- báo cáo validation phục vụ CI.

Mỗi signal record phải có: key, PGN, SPN, name, unit, start byte/bit, bit length, data kind, resolution, offset, min/max và unavailable rule.

Mỗi PGN record phải có: logical payload length, frame mode, priority, source address, transmission mode, nominal period, fallback/max period và danh sách signal key.

### 3.3 Mô hình runtime FW

- Metadata bất biến nằm trong Flash.
- Active signal state là cấu trúc compact; mục tiêu không quá 48 byte/signal.
- PGN runtime state giữ `next_due_ms`, mode, dirty flag, last payload/hash và counters.
- Không cấp phát heap trong đường truyền CAN.
- Pattern chỉ được tính khi PGN đến hạn hoặc khi điều kiện event cần đánh giá.
- Scalar codec dùng raw integer tối đa 32 bit; ASCII/blob dùng API riêng.

### 3.4 Scheduler và TX queue

- Scheduler dùng deadline tuyệt đối: `next_due += period`, không dùng `last_tx = now`.
- Gán phase offset khi start để dàn đều PGN có cùng chu kỳ.
- Queue phần mềm có 32 frame; mỗi entry chứa CAN ID, DLC, data, class và deadline.
- Tx-complete/poll sẽ refill ba mailbox mà không block main loop.
- Priority queue:
  1. control ACK và PGN periodic 10/20 ms;
  2. periodic còn lại và event;
  3. TP.DT/background.
- Khi queue đầy: tăng counter, báo lỗi profile/runtime; không âm thầm abort mailbox.

## 4. Control protocol đồng bộ UI–FW

### 4.1 Versioning

- Protocol đích: major `2`, schema `2`.
- FW phải công bố protocol version, capabilities 32 bit, catalog CRC32 và runtime capacities.
- UI chỉ cho phép cấu hình khi major/schema/catalog CRC phù hợp.
- Trong giai đoạn chuyển tiếp, giữ đường v1 cho EEC1 để regression test; xóa khi v2 đạt hardware gate.

### 4.2 Transaction cấu hình single-frame

Giữ format control frame 8 byte hiện tại và thêm transaction:

1. `BEGIN_CONFIG(expected_count, catalog_crc32)` — tạo staging profile khi FW đang STOPPED.
2. `SET_CONST(signal_key, raw_value)` — thêm/cập nhật scalar trong staging profile.
3. `SET_PGN_PERIOD(pgn_key, period_ms)` — chỉ dùng cho override được cho phép ở mức PGN.
4. `COMMIT_CONFIG` — validate toàn bộ rồi swap staging thành active profile.
5. `ABORT_CONFIG` — bỏ staging và giữ active profile cũ.
6. `START` — chỉ hợp lệ sau một commit thành công.
7. `STOP` — idempotent, dừng phát nhưng giữ committed profile.
8. `TRIGGER_PGN` — phục vụ on-request/test có kiểm soát.

Mọi command tiếp tục dùng sequence, retry cùng payload và dedup cache. Nếu bất kỳ signal nào sai key, range, duplicate, overlap, capacity hoặc schedule, `COMMIT_CONFIG` phải thất bại và active profile không thay đổi.

### 4.3 Status và diagnostics

UI phải đọc được tối thiểu:

- active signal/PGN count;
- catalog CRC và protocol/schema;
- runtime state: stopped/configuring/ready/running/fault;
- TX queued/sent/busy/error/drop và queue high-water;
- scheduler deadline miss;
- TP active/completed/aborted/timeout;
- current estimated bus load.

## 5. Các phase triển khai

## Phase 0 — Baseline có thể tái lập

### FW

- Tạo lệnh build/test host chuẩn, không phụ thuộc executable cũ.
- Lưu báo cáo Flash/RAM từ linker map trong CI artifact.
- Thêm test tái hiện các lỗi scheduler period 0 và đăng ký không atomic.

### UI

- Chuẩn hóa virtual environment và cài đúng PySide6.
- Chạy toàn bộ unit test không cần hardware.
- Tách frame logging khỏi GUI rendering.
- Khóa production transport vào TSMaster Native API và xác nhận một worker thread là owner duy nhất của session.

### Gate P0

- FW build thành công và toàn bộ host-test xanh.
- UI test xanh 100%.
- Golden trace v1 EEC1 vẫn chạy không thay đổi.
- Control request/ACK và EEC1 readback được xác nhận đi trên cùng CAN channel/hardware mapping.

## Phase 1 — Catalog chung và address model

### Shared

- Viết catalog generator và validator.
- Sinh C + JSON/Python + manifest trong cùng một run.
- Validation: count, unique `(PGN,SPN)`, stable key, overlap, payload length, duplicated SPN, fixed/event rate.

### FW

- Thay timing table thủ công bằng generated PGN catalog.
- Tách control node address khỏi virtual ECU source address.

### UI

- Đọc generated catalog.
- Tách control address và filter source address theo từng PGN/virtual ECU.
- Hiển thị catalog CRC và từ chối catalog mismatch.

### Gate P1

- Đúng 179 records, 60 PGN, 57 single-frame, 3 TP.
- EEC1 phát từ SA `0x00` được UI decode trong khi control ACK vẫn từ `0x80`.
- Hai SPN trùng số được phân biệt bằng PGN.

## Phase 2 — Multi-signal transaction và vertical slice ba signal

### FW

- Implement protocol v2 negotiation và config transaction.
- Mở stable key cho toàn catalog nhưng chỉ enable ba signal đầu tiên.
- Validate/commit atomic; thêm rollback test.

### UI

- Implement v2 handshake và transaction client.
- Cấu hình đồng thời Engine Speed, Accelerator Pedal Position và Vehicle Speed.
- START chỉ sau COMMIT ACK.

### Gate P2

- Retry không tạo cấu hình trùng.
- Commit lỗi không thay đổi active profile.
- Ba signal phát/đọc lại đúng golden vector và đúng chu kỳ PGN.

## Phase 3 — Scheduler, TX queue và scale 40/100 SPN

### FW

- Thay scheduler hiện tại bằng PGN-centric scheduler.
- Thêm phase spreading, software TX queue 32 frame và diagnostics.
- Compact active state; tính waveform lazy.
- Enable lần lượt 40 rồi 100 active SPN thuộc các PGN periodic single-frame.
- Thêm profile admission check theo RAM, PGN count và bus load.

### UI

- Thêm bảng chọn nhiều signal và batch configuration progress.
- Hiển thị estimated bus load trước COMMIT.
- Cache readback theo signal key; refresh bảng 5–10 Hz.
- Có chế độ raw capture riêng; log GUI mặc định chỉ hiển thị event/error/summary.

### Gate P3-A — 40 SPN

- Chạy 60 phút tại 250 kbit/s, không drop/queue overflow/deadline miss.
- Giá trị, PGN grouping, DLC và period khớp catalog.

### Gate P3-B — 100 SPN

- RAM `data+bss <= 17 KB`, Flash `<= 56 KB`.
- Stack watermark còn ít nhất 1.5 KB.
- Chạy 100 active SPN/đến 60 PGN trong 60 phút.
- Queue high-water dưới 75%, không abort mailbox và không mất frame nội bộ.
- UI vẫn responsive ở khoảng 700 frame/s.

## Phase 4 — Hoàn thiện 57 PGN single-frame

### FW

- Enable đủ 45 PGN periodic.
- Implement on-request qua Request PGN `0xEA00`.
- Implement on-change với minimum interval và maximum/fallback period.
- Giữ engine-speed-dependent và `On request (100 ms)` ở `UNRESOLVED` cho đến khi có requirement.

### UI

- Hỗ trợ trigger/request PGN.
- Hiển thị rõ periodic/on-request/on-change/unresolved.
- Không cho người dùng START profile chứa rule unresolved.

### Gate P4

- Tất cả 57 PGN single-frame có golden payload test.
- Toàn bộ 175 record single-frame encode/decode round-trip đúng.
- Không PGN nào phát một frame riêng cho từng SPN.

## Phase 5 — J1939 TP cho payload >8 byte

### FW transport layer

- Buffer logic tối đa 200 byte; unused byte được fill `0xFF`.
- BAM TX state machine cho broadcast.
- Sau khi BAM ổn định, thêm RTS/CTS, EOM_ACK, ABORT và timeout.
- Một outgoing và một incoming TP session tại một thời điểm.
- Serialize session theo `(SA, DA, PGN)` và đi qua chung TX queue.

### PGN cần hỗ trợ

- FEE1: 18 byte, BAM control + 3 TP.DT.
- FEE3: 21 byte, BAM control + 3 TP.DT.
- FEEC: tối đa 200 byte, control + tối đa 29 TP.DT; xác nhận lại chiều dài VIN nghiệp vụ trước khi release.

### UI

- Reassemble TP.CM/TP.DT, kiểm tra sequence, length, PGN và timeout.
- Decode scalar/string sau khi nhận đủ logical payload.
- Hiển thị trạng thái session và lỗi packet.

### Gate P5

- Golden tests cho logical length 8, 9, 18, 21 và 200 byte.
- Packet loss, duplicate, out-of-order và timeout đều kết thúc session an toàn.
- TP background không làm miss deadline PGN 10/20 ms.

## Phase 6 — Network management và product hardening

- Quyết định mô hình một ECU hay nhiều virtual ECU.
- Nếu dùng ngoài bench cô lập, implement hoặc tích hợp J1939-81 Address Claim cho từng virtual ECU NAME/SA.
- Bổ sung soak test 8–24 giờ, bus-off recovery, power-cycle, config interruption và catalog mismatch.
- Khóa release theo cặp version FW/UI; không phát hành riêng một phía khi protocol/catalog thay đổi.

## 6. Bus-load policy

- UI và FW đều phải tính tải từ PGN, period, frame mode và TP overhead.
- Mạng bench cô lập: cảnh báo tại 50%, mặc định reject profile trên 70%.
- Mạng chia sẻ với DUT/OBU: mặc định giới hạn generator ở 30%, trừ khi người kiểm thử xác nhận budget khác.
- Admission check phải dùng số PGN/frame thực tế, không dùng số SPN.
- Stress mode không được bỏ qua admission check; phải là chế độ có cảnh báo rõ và yêu cầu thao tác chủ động.

## 7. Test matrix bắt buộc

### Data/catalog

- 179 records, 177 SPN numbers, 60 PGN.
- Duplicate SPN 928 và 1807 được key bằng PGN.
- 57 PGN ≤8 byte, 3 PGN >8 byte.
- SPN overlap, invalid position, invalid bit width và unresolved rate bị phát hiện.

### Codec

- Aligned/unaligned 2, 4, 8, 12, 16 và 32 bit.
- Resolution/offset/range/unavailable raw.
- Signal nằm ở byte >8 được encode vào logical buffer, không vào frame 8 byte.
- ASCII/blob không đi qua scalar codec.

### Protocol

- Handshake compatible/incompatible/catalog mismatch.
- Retry cùng sequence/payload; duplicate replay và conflict.
- BEGIN/SET/COMMIT thành công.
- Invalid record, table full, timeout và ABORT giữ nguyên active profile cũ.

### Scheduler/TX

- Millisecond counter wraparound.
- Period 0 không phát tự động.
- Multiple PGN cùng period được phase-spread.
- Queue full có counter/fault rõ ràng, không abort frame khác.
- STOP chặn enqueue mới và giữ profile đã commit.

### Hardware-in-the-loop

- 3, 40 và 100 active SPN tại 250/500 kbit/s.
- So sánh frame count, period, jitter và payload với TSMaster trace.
- Mất ACK, disconnect/reconnect, bus-off/recovery và reset giữa config transaction.
- TP BAM/RTS/CTS với payload 18/21/200 byte.

## 8. Definition of Done toàn dự án

- UI và FW được build từ cùng catalog/schema và xác minh CRC lúc handshake.
- 100 active SPN chạy ổn định trên STM32F103C8 trong resource budget.
- 175 single-frame records có automated round-trip test.
- Ba long PGN đi qua TP đúng chuẩn và không ảnh hưởng deadline của periodic traffic.
- Không còn hard-code EEC1 riêng trong core worker/UI decoder.
- Không có silent drop; mọi overload, timeout và mismatch đều có counter và thông báo.
- Release package gồm FW binary/map, UI package, catalog manifest, test report và TSMaster trace chuẩn.

## 9. Thứ tự thực hiện ngay

1. Hoàn thành P0 và sửa test environment.
2. Thực hiện catalog generator + address model của P1.
3. Chốt protocol v2 transaction và hoàn thành vertical slice ba signal P2.
4. Refactor scheduler/TX queue rồi chạy gate 40 SPN.
5. Tối ưu RAM/CPU và chạy gate 100 SPN.
6. Enable đủ 57 PGN single-frame.
7. Implement BAM, sau đó RTS/CTS cho ba PGN dài.
8. Chỉ sau các gate trên mới bổ sung Address Claim và hardening release.
