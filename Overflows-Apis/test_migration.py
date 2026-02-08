#!/usr/bin/env python3
"""
Migration helper script to compare old vs new API responses
"""

import requests
import json
from typing import Dict, Any

OLD_API = "http://localhost:8000"
NEW_API = "http://localhost:8001"

def compare_responses(old_response: Dict[Any, Any], new_response: Dict[Any, Any]) -> bool:
    """Compare two API responses for compatibility."""
    if old_response.keys() != new_response.keys():
        print(f"❌ Key mismatch:")
        print(f"   Old keys: {old_response.keys()}")
        print(f"   New keys: {new_response.keys()}")
        return False
    
    for key in old_response.keys():
        if old_response[key] != new_response[key]:
            print(f"⚠️  Value difference for '{key}':")
            print(f"   Old: {old_response[key]}")
            print(f"   New: {new_response[key]}")
    
    return True

def test_endpoint(path: str, method: str = "GET", data: Dict = None):
    """Test an endpoint on both versions."""
    print(f"\n{'='*80}")
    print(f"Testing: {method} {path}")
    print(f"{'='*80}")
    
    try:
        # Test old API
        if method == "GET":
            old_resp = requests.get(f"{OLD_API}{path}")
        else:
            old_resp = requests.post(f"{OLD_API}{path}", json=data)
        
        print(f"✅ Old API: {old_resp.status_code}")
        
        # Test new API
        if method == "GET":
            new_resp = requests.get(f"{NEW_API}{path}")
        else:
            new_resp = requests.post(f"{NEW_API}{path}", json=data)
        
        print(f"✅ New API: {new_resp.status_code}")
        
        if old_resp.status_code == new_resp.status_code == 200:
            old_data = old_resp.json()
            new_data = new_resp.json()
            
            if compare_responses(old_data, new_data):
                print(f"✅ Responses match!")
            else:
                print(f"⚠️  Responses differ (may be expected)")
        
    except requests.exceptions.ConnectionError as e:
        print(f"❌ Connection error: {e}")
        print(f"   Make sure both APIs are running:")
        print(f"   - Old: python animation_api.py (port 8000)")
        print(f"   - New: python animation_api_modular.py (port 8001)")
    except Exception as e:
        print(f"❌ Error: {e}")

def main():
    """Run comparison tests."""
    print("\n" + "="*80)
    print("API Migration Compatibility Checker")
    print("="*80)
    
    # Test root endpoint
    test_endpoint("/")
    
    # Test health check (new endpoint)
    test_endpoint("/health")
    
    # Test jobs list
    test_endpoint("/jobs")
    
    # Add more tests as needed
    print("\n" + "="*80)
    print("Testing complete!")
    print("="*80)

if __name__ == "__main__":
    main()
