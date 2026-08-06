# Google Maps Lead UI

Giao diện nhập **ngành nghề + khu vực**, tạo job trên `gosom/google-maps-scraper`, tự theo dõi trạng thái và xuất:

- Excel gọn
- CSV gọn
- Excel đầy đủ
- CSV đầy đủ

Bản gọn giữ các cột thường dùng cho CRM: tên doanh nghiệp, ngành nghề, địa chỉ, điện thoại, website, email, rating, tọa độ, Google Maps, Place ID và CID. Dữ liệu được loại trùng ưu tiên theo `place_id`, sau đó tới số điện thoại. Bản gọn mặc định cũng lọc các dòng có địa chỉ nằm ngoài khu vực chính; bản đầy đủ luôn giữ nguyên dữ liệu thô.

## Triển khai trên server hiện tại

```bash
cd /opt/google-maps-scraper
mkdir -p lead-ui
```

Chép toàn bộ nội dung project này vào `/opt/google-maps-scraper/lead-ui`, sau đó dùng file compose ở thư mục cha hoặc thay `build: .` thành `build: ./lead-ui`.

Cấu trúc khuyến nghị:

```text
/opt/google-maps-scraper/
├── compose.yaml
├── data/
└── lead-ui/
    ├── Dockerfile
    ├── requirements.txt
    └── app/
```

Trong `/opt/google-maps-scraper/compose.yaml`, service `lead-ui` phải có:

```yaml
lead-ui:
  build: ./lead-ui
  environment:
    SCRAPER_BASE_URL: http://google-maps-scraper:8080
    APP_USERNAME: admin
    APP_PASSWORD: MAT_KHAU_MANH
  ports:
    - "8081:8000"
```

Chạy:

```bash
docker compose up -d --build
docker compose ps
docker compose logs -f --tail=200 lead-ui
```

Truy cập:

```text
http://SERVER_IP:8081
```

Nếu dùng Google Cloud, mở firewall TCP 8081. Sau khi ổn định, nên chỉ public Nginx 80/443 và bỏ publish trực tiếp 8080 của scraper.

## API wrapper

Tạo job:

```bash
curl -u 'admin:MAT_KHAU_MANH' \
  -X POST http://SERVER_IP:8081/api/jobs \
  -H 'Content-Type: application/json' \
  -d '{
    "industry":"spa",
    "area":"TP Hồ Chí Minh",
    "subareas":["Quận 1","Quận 3","Quận 7"],
    "depth":1,
    "email":false,
    "extra_reviews":false,
    "max_time":600
  }'
```

Xem trạng thái:

```bash
curl -u 'admin:MAT_KHAU_MANH' http://SERVER_IP:8081/api/jobs/JOB_ID
```

Tải Excel gọn:

```bash
curl -u 'admin:MAT_KHAU_MANH' -L \
  -o leads.xlsx \
  'http://SERVER_IP:8081/api/jobs/JOB_ID/download?format=xlsx&mode=clean'
```
