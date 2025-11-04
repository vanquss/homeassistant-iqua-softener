"""
Simple diagnostic script to check iQua library status and capabilities.
Run this to see what methods are available and test basic functionality.
"""

def check_iqua_library():
    """Check if iqua_softener library is available and what methods it has."""
    try:
        from iqua_softener import IquaSoftener
        print("✓ iqua_softener library imported successfully")
        
        # Create a dummy instance to check available methods
        try:
            softener = IquaSoftener("test", "test", "test")
            methods = [method for method in dir(softener) if not method.startswith('_')]
            print(f"✓ IquaSoftener class created, available methods:")
            for method in sorted(methods):
                print(f"  - {method}")
                
            # Check for specific WebSocket methods
            if hasattr(softener, 'get_websocket_uri'):
                print("✓ get_websocket_uri method is available")
            else:
                print("✗ get_websocket_uri method is NOT available")
                
            if hasattr(softener, 'update_external_realtime_data'):
                print("✓ update_external_realtime_data method is available")
            else:
                print("✗ update_external_realtime_data method is NOT available")
                
        except Exception as e:
            print(f"✗ Failed to create IquaSoftener instance: {e}")
            
    except ImportError as e:
        print(f"✗ Failed to import iqua_softener: {e}")
        print("  Make sure the library is installed in your Home Assistant environment")

if __name__ == "__main__":
    print("iQua Library Diagnostic")
    print("=" * 30)
    check_iqua_library()