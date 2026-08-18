# src/fpl_mcp/cli.py
import os
import sys
import argparse
import getpass
import asyncio
from pathlib import Path

from .fpl.credential_manager import extract_refresh_token


def _read_long_secret(prompt):
    """Read a possibly very long secret line from the terminal.

    getpass()/input() read the tty in canonical mode, which on macOS/BSD caps a
    single input line at MAX_CANON (~1024 bytes). The OIDC refresh token is over
    1KB, so pasting it into a normal prompt fills that buffer and freezes the
    terminal. Switching the tty to non-canonical mode removes the line limit so
    arbitrarily long pastes are read reliably.
    """
    # Piped / non-interactive stdin: a plain read has no canonical limit.
    if not sys.stdin.isatty():
        return sys.stdin.readline().strip()

    try:
        import termios
    except ImportError:
        # Non-POSIX (e.g. Windows): getpass is fine there.
        return getpass.getpass(prompt).strip()

    sys.stdout.write(prompt)
    sys.stdout.flush()

    fd = sys.stdin.fileno()
    old = termios.tcgetattr(fd)
    new = termios.tcgetattr(fd)
    new[3] = new[3] & ~(termios.ICANON | termios.ECHO)  # lflags
    new[6][termios.VMIN] = 1  # cc: block until at least 1 byte
    new[6][termios.VTIME] = 0

    buf = b""
    try:
        termios.tcsetattr(fd, termios.TCSANOW, new)
        while True:
            chunk = os.read(fd, 4096)
            if not chunk:
                break
            if b"\x03" in chunk:  # Ctrl-C
                raise KeyboardInterrupt
            terminators = [i for i in (chunk.find(b"\n"), chunk.find(b"\r")) if i != -1]
            if terminators:
                buf += chunk[: min(terminators)]
                break
            buf += chunk
    finally:
        termios.tcsetattr(fd, termios.TCSADRAIN, old)
        sys.stdout.write("\n")
        sys.stdout.flush()

    # Strip bracketed-paste markers if the terminal emitted them.
    for marker in (b"\x1b[200~", b"\x1b[201~"):
        buf = buf.replace(marker, b"")
    return buf.decode("utf-8", "replace").strip()


def setup_credentials():
    """Interactive CLI for setting up FPL credentials with encryption"""
    print("FPL MCP Server - Credential Setup")
    print("=================================")
    print("FPL now uses PingOne (OIDC) login, so this tool authenticates with an")
    print("OIDC refresh token instead of your email/password.")
    print()
    print("To get your refresh token:")
    print("  1. Log in at https://fantasy.premierleague.com in your browser.")
    print("  2. Open the DevTools Console (F12 -> Console) and run:")
    print()
    print("     copy(JSON.parse(localStorage.getItem(Object.keys(localStorage).find(k=>k.startsWith('oidc.user:')))).refresh_token)")
    print()
    print("     (If Chrome refuses, type 'allow pasting' in the console first.)")
    print("  3. The refresh token is now on your clipboard; paste it below.")
    print()
    print("  Alternatively: DevTools -> Application -> Local storage ->")
    print("  https://fantasy.premierleague.com -> copy the whole value of the key")
    print("  starting with 'oidc.user:' (the refresh token will be extracted).")
    print()
    print("Your token will be encrypted and stored securely in ~/.fpl-mcp/")
    print()

    # Get credentials. The pasted value can exceed the terminal's canonical-mode
    # line limit, so read it in non-canonical mode to avoid a frozen paste.
    pasted = _read_long_secret("Paste the oidc.user JSON (or just the refresh token): ")
    refresh_token = extract_refresh_token(pasted)
    if not refresh_token:
        print(
            "Error: could not find a refresh token in the pasted value. "
            "Paste the full oidc.user JSON or its 'refresh_token' field."
        )
        return False

    team_id = input("Enter your FPL team ID: ").strip()
    if not team_id:
        print("Error: team ID is required.")
        return False

    try:
        # Import credential manager
        from .fpl.credential_manager import CredentialManager

        # Initialize credential manager and store credentials
        credential_manager = CredentialManager()
        credential_manager.store_credentials(refresh_token, team_id)
        
        print("\nCredentials encrypted and saved successfully!")
        print("Your refresh token is now stored securely using encryption.")
        print("Run 'fpl-mcp-config test' now to validate it — the stored token can")
        print("be superseded by your browser session, so don't wait too long.")

        # Check if legacy credentials exist and offer to clean them up
        legacy_files = []
        config_dir = Path.home() / ".fpl-mcp"
        
        if (config_dir / ".env").exists():
            legacy_files.append(str(config_dir / ".env"))
        if (config_dir / "config.json").exists():
            legacy_files.append(str(config_dir / "config.json"))
            
        if legacy_files:
            print("\nLegacy credential files detected:")
            for file in legacy_files:
                print(f"  - {file}")
            print("\nThese files contain plaintext credentials and are no longer needed.")
            remove_legacy = input("Would you like to remove them? (y/N): ").lower()
            
            if remove_legacy == 'y':
                for file in legacy_files:
                    try:
                        os.remove(file)
                        print(f"Removed: {file}")
                    except Exception as e:
                        print(f"Could not remove {file}: {e}")
        
        print("Configuration successful!")
        return True
        
    except Exception as e:
        print(f"Error saving encrypted credentials: {e}")
        print("You may need to install the cryptography library: pip install cryptography")
        return False

async def login_with_browser(
    username: str = "",
    password: str = "",
    team_id: str = "",
    headless: bool = True,
    save_credentials: bool = False,
):
    """Log in via a real browser to obtain and store a refresh token.

    Automates the manual 'copy oidc.user from DevTools' step: drives the
    PingOne login form with a username/password, reads the refresh token the
    web app stores in localStorage, auto-discovers the team id, validates the
    token against the FPL API, and stores it encrypted.
    """
    from .fpl.auth_manager import get_auth_manager
    from .fpl.browser_login import (
        LoginError,
        login as browser_login,
        resolve_credentials,
        store_login_credentials,
    )

    print("FPL MCP Server - Browser Login")
    print("==============================")

    # Resolve credentials from args, then keyring, then env vars.
    try:
        username, password = resolve_credentials(username or None, password or None)
    except LoginError:
        # Prompt interactively as a last resort.
        if not username:
            username = input("FPL email: ").strip()
        if not password:
            password = getpass.getpass("FPL password: ").strip()
        if not (username and password):
            print("Error: username and password are required.")
            return False

    if save_credentials:
        try:
            store_login_credentials(username, password)
            print("Saved credentials to the system keyring for future logins.")
        except LoginError as e:
            print(f"Warning: could not save credentials to keyring: {e}")

    print("Launching browser to complete the PingOne login...")
    try:
        result = browser_login(username=username, password=password, headless=headless)
    except LoginError as e:
        print(f"Login failed: {e}")
        return False

    refresh_token = result.refresh_token
    print(f"Obtained refresh token ({len(refresh_token)} chars).")

    auth_manager = get_auth_manager()

    # Discover the team id from /api/me/ if not supplied.
    resolved_team_id = team_id.strip() or auth_manager.team_id
    if not resolved_team_id:
        auth_manager.set_credentials(refresh_token, "0")  # temp, to enable /me/
        try:
            resolved_team_id = await auth_manager.discover_team_id()
        except Exception as e:
            print(f"Could not auto-discover team id: {e}")
            resolved_team_id = None

    if not resolved_team_id:
        resolved_team_id = input("Enter your FPL team ID: ").strip()
    if not resolved_team_id:
        print("Error: team ID is required.")
        return False

    # Store and validate. set_credentials clears the session so the next call
    # re-authenticates with the freshly stored token.
    auth_manager.set_credentials(refresh_token, str(resolved_team_id))
    try:
        team_data = await auth_manager.get_my_team()
    except Exception as e:
        print(f"Token was stored but failed validation: {e}")
        return False

    print("\nLogin successful! Credentials encrypted and saved to ~/.fpl-mcp/")
    print(f"Team ID: {resolved_team_id}")
    print(f"Squad picks returned: {len(team_data.get('picks', []))}")
    return True


async def test_auth():
    """Test authentication with FPL API"""
    try:
        # Import here to avoid circular imports
        from .fpl.auth_manager import get_auth_manager
        
        auth_manager = get_auth_manager()

        if not auth_manager._refresh_token or not auth_manager.team_id:
            print(
                "Authentication is not configured. "
                "Run 'fpl-mcp-config setup' to add your refresh token and team ID."
            )
            return False

        # /my-team/ rejects requests without a valid X-API-Authorization
        # header, so this proves the API accepts our token end-to-end
        # (/entry/ is public and would succeed even with a bad token).
        team_data = await auth_manager.get_my_team()
        entry_data = await auth_manager.get_entry_data()

        print("Authentication successful!")
        print(f"Team name: {entry_data.get('name', 'Unknown')}")
        print(f"Manager: {entry_data.get('player_first_name', '')} {entry_data.get('player_last_name', '')}")
        print(f"Overall rank: {entry_data.get('summary_overall_rank', 'Unknown')}")
        print(f"Squad picks returned: {len(team_data.get('picks', []))}")
        return True
    except Exception as e:
        print(f"Authentication failed: {e}")
        return False
    finally:
        # Clean up resources
        try:
            await auth_manager.close()
        except:
            pass

def main():
    parser = argparse.ArgumentParser(description="FPL MCP Server Configuration")
    subparsers = parser.add_subparsers(dest="command", help="Command to run")
    
    subparsers.add_parser("setup", help="Set up FPL credentials (paste refresh token)")
    subparsers.add_parser("test", help="Test FPL authentication")

    login_parser = subparsers.add_parser(
        "login",
        help="Log in with username/password via a browser to obtain a refresh token",
    )
    login_parser.add_argument(
        "-u", "--username", default="", help="FPL email (else keyring/env/prompt)"
    )
    login_parser.add_argument(
        "-p", "--password", default="", help="FPL password (else keyring/env/prompt)"
    )
    login_parser.add_argument(
        "-t", "--team-id", default="", help="FPL team ID (auto-discovered if omitted)"
    )
    login_parser.add_argument(
        "--no-headless",
        action="store_true",
        help="Show the browser window (useful to solve a bot challenge)",
    )
    login_parser.add_argument(
        "--save-credentials",
        action="store_true",
        help="Save username/password to the system keyring for future logins",
    )

    args = parser.parse_args()

    if args.command == "setup":
        setup_credentials()
    elif args.command == "test":
        asyncio.run(test_auth())
    elif args.command == "login":
        asyncio.run(
            login_with_browser(
                username=args.username,
                password=args.password,
                team_id=args.team_id,
                headless=not args.no_headless,
                save_credentials=args.save_credentials,
            )
        )
    else:
        parser.print_help()

if __name__ == "__main__":
    main()