# TradeX v4 API - Swagger & Documentation

## Server Status

✅ **API Server Running in Background**
- **PID**: 95845
- **Host**: http://127.0.0.1:8080
- **Broker**: Dhan (Paper mode)
- **Authorization**: DISABLED (all endpoints public)
- **Logs**: `/tmp/tradex-api.log`

---

## 📚 API Documentation Endpoints

All three standard FastAPI documentation endpoints are available:

### 1. Swagger UI (Interactive)
**URL**: http://127.0.0.1:8080/docs

- ✅ Interactive API explorer
- ✅ Try out endpoints directly from browser
- ✅ View request/response schemas
- ✅ Test all operations without authentication

**Access**: Open in any web browser
```bash
# Or test with curl
curl http://127.0.0.1:8080/docs
```

### 2. ReDoc (Alternative Documentation)
**URL**: http://127.0.0.1:8080/redoc

- ✅ Clean, readable API documentation
- ✅ Three-column layout (navigation, docs, examples)
- ✅ Better for reading than testing
- ✅ Auto-generated from OpenAPI spec

**Access**: Open in any web browser
```bash
curl http://127.0.0.1:8080/redoc
```

### 3. OpenAPI Specification (JSON)
**URL**: http://127.0.0.1:8080/openapi.json

- ✅ Complete API specification in JSON format
- ✅ Can be imported into API clients (Postman, Insomnia, etc.)
- ✅ Machine-readable format for code generation
- ✅ Version: OpenAPI 3.1.0

**Access**:
```bash
# Download full spec
curl http://127.0.0.1:8080/openapi.json > tradex-api-spec.json

# Pretty-print it
curl http://127.0.0.1:8080/openapi.json | python -m json.tool
```

---

## 🔍 Available API Groups

Based on the OpenAPI spec, these endpoint groups are documented:

### Market Data
- `GET /quotes/{exchange}:{symbol}` - Real-time quotes
- `GET /search` - Search instruments
- `GET /history/{instrument_id}` - Historical bars
- `GET /option-chain/{underlying}` - Option chain
- `GET /future-chain/{underlying}` - Future chain

### Portfolio
- `GET /positions` - Current positions
- `GET /holdings` - Portfolio holdings

### Orders
- `GET /orders` - List orders
- `GET /orders/{order_id}` - Get order details
- `POST /orders` - Place order
- `POST /orders/bracket` - Place bracket order
- `PUT /orders/{order_id}` - Modify order
- `DELETE /orders/{order_id}` - Cancel order

### Account
- `GET /account` - Account information

### Charts
- `GET /api/charts/symbols` - List available symbols
- Various chart-related endpoints

### Health & System
- `GET /health` - Health check
- `GET /health/live` - Liveness probe
- `GET /health/ready` - Readiness probe
- `GET /extensions` - Broker capabilities

---

## 🧪 Quick Testing

### Using Swagger UI (Recommended)

1. Open your browser to: http://127.0.0.1:8080/docs
2. Click on any endpoint to expand it
3. Click "Try it out" button
4. Fill in parameters (if any)
5. Click "Execute"
6. View response immediately

### Using cURL

```bash
# Health check
curl http://127.0.0.1:8080/health

# Get account info
curl http://127.0.0.1:8080/account

# Search for instruments
curl "http://127.0.0.1:8080/search?q=RELIANCE"

# Get quote
curl http://127.0.0.1:8080/quotes/NSE:RELIANCE

# Check positions
curl http://127.0.0.1:8080/positions

# List orders
curl http://127.0.0.1:8080/orders
```

### Using Postman/Insomnia

1. Import the OpenAPI spec:
   ```bash
   curl http://127.0.0.1:8080/openapi.json > tradex-api.json
   ```
2. Import `tradex-api.json` into your API client
3. All endpoints will be pre-configured
4. No authentication headers needed!

---

## 📊 API Information

**Title**: TradeX v4 API  
**Version**: 0.1.0  
**OpenAPI Version**: 3.1.0  
**Base URL**: http://127.0.0.1:8080  

### Authentication
**Status**: DISABLED  
No API keys, tokens, or session cookies required.

### CORS
Configured for local development (127.0.0.1)

---

## 🛠️ Server Management

### Check if server is running
```bash
ps aux | grep "python.*serve" | grep -v grep
```

### View logs
```bash
tail -f /tmp/tradex-api.log
```

### Stop server
```bash
kill 95845
```

### Restart server
```bash
cd /Users/apple/Downloads/v2-cleanup-remove-dead-temp-legacy-2026-07-31/v4
source .env.local
nohup python interfaces/src/tradex_interfaces/cli.py serve --host 127.0.0.1 --port 8080 --broker dhan > /tmp/tradex-api.log 2>&1 &
```

---

## 🎯 Next Steps

1. **Explore the API**: Visit http://127.0.0.1:8080/docs in your browser
2. **Test endpoints**: Use Swagger UI or cURL to try different operations
3. **Import spec**: Load OpenAPI JSON into Postman for easier testing
4. **Check logs**: Monitor `/tmp/tradex-api.log` for any issues

---

**Documentation Generated**: November 9, 2026  
**Server Status**: ✅ Running with Swagger/OpenAPI enabled
