import asyncio
from src.hohsin_api import HohsinAPI
async def list_stns():
    api = HohsinAPI()
    stations = await api.get_stations()
    print("--- 和欣客運車站代碼清單 ---")
    for s in sorted(stations, key=lambda x: x['id']):
        print(f"[{s['id']}] {s['operatingName']}")
    await api.close()
asyncio.run(list_stns())
