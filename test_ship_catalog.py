#!/usr/bin/env python3
"""
Test script to check ship catalog loading.
"""

import asyncio
import websockets
import json

async def test_ship_catalog():
    """Test ship catalog loading."""
    
    print("🧪 Testing Ship Catalog Loading")
    print("=" * 40)
    
    try:
        # Connect to debug WebSocket
        uri = "ws://localhost:8000/ws/debug"
        async with websockets.connect(uri) as websocket:
            print("✅ Connected to debug WebSocket")
            
            # Send debug restart to reload catalog
            restart_cmd = {
                "topic": "debug.restart",
                "data": {}
            }
            
            print("🔄 Sending debug restart to reload ship catalog...")
            await websocket.send(json.dumps(restart_cmd))
            
            # Wait for telemetry
            await asyncio.sleep(3)
            
            # Check ship inventory
            for _ in range(5):
                try:
                    response = await asyncio.wait_for(websocket.recv(), timeout=1.0)
                    data = json.loads(response)
                    if data.get("topic") == "telemetry":
                        ships = data.get("data", {}).get("ships", [])
                        for ship in ships:
                            if ship.get("id") == "red-dd-dc-01":
                                weapons = ship.get("weapons", {})
                                dc_stored = weapons.get("depth_charges_stored", 0)
                                dc_cooldown = weapons.get("depth_charge_cooldown_s", 0)
                                print(f"📊 Ship {ship.get('id')}: depth_charges_stored={dc_stored}, cooldown={dc_cooldown}s")
                                break
                        break
                except asyncio.TimeoutError:
                    continue
            
            print("✅ Test completed")
            
    except Exception as e:
        print(f"❌ Error: {e}")

if __name__ == "__main__":
    asyncio.run(test_ship_catalog())
