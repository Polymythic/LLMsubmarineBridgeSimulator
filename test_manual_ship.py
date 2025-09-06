#!/usr/bin/env python3
"""
Test manual ship creation to debug depth charges.
"""

import asyncio
import websockets
import json

async def test_manual_ship():
    """Test manual ship creation."""
    
    print("🧪 Testing Manual Ship Creation")
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
            
            print("🔄 Sending debug restart...")
            await websocket.send(json.dumps(restart_cmd))
            
            # Wait for telemetry
            await asyncio.sleep(3)
            
            # Check all ships
            for _ in range(3):
                try:
                    response = await asyncio.wait_for(websocket.recv(), timeout=1.0)
                    data = json.loads(response)
                    if data.get("topic") == "telemetry":
                        ships = data.get("data", {}).get("ships", [])
                        print(f"📊 Found {len(ships)} ships:")
                        for ship in ships:
                            weapons = ship.get("weapons", {})
                            capabilities = ship.get("capabilities", {})
                            dc_stored = weapons.get("depth_charges_stored", 0)
                            has_dc = capabilities.get("has_depth_charges", False)
                            print(f"  {ship.get('id')} ({ship.get('class')}): depth_charges_stored={dc_stored}, has_depth_charges={has_dc}")
                        break
                except asyncio.TimeoutError:
                    continue
            
            print("✅ Test completed")
            
    except Exception as e:
        print(f"❌ Error: {e}")

if __name__ == "__main__":
    asyncio.run(test_manual_ship())
