"""本地启动入口；生产环境也可直接使用 ``uvicorn app.main:app``。"""

import uvicorn


if __name__ == "__main__":
    uvicorn.run("app.main:app", host="127.0.0.1", port=8000, reload=True)
