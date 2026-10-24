# Deploying on PythonAnywhere

This app can run on PythonAnywhere's free web-app tier with SQLite. Keep the
database in your account's home directory, outside the application checkout,
so code updates do not overwrite it.

## Configure the account

1. Create a PythonAnywhere account and clone this repository into your home
   directory, for example `/home/<username>/iskcon-book-inventory`.
2. In a Bash console, create a virtual environment and install the requirements:

   ```bash
   cd ~/iskcon-book-inventory
   python3 -m venv ~/.virtualenvs/iskcon-book-inventory
   ~/.virtualenvs/iskcon-book-inventory/bin/pip install -r requirements.txt
   mkdir -p ~/iskcon-book-inventory-data
   ```

3. In the **Web** tab, create a **Manual configuration** web app using a Python
   version supported by the account. Set its virtualenv to
   `/home/<username>/.virtualenvs/iskcon-book-inventory`.
4. Edit the WSGI configuration file linked from the Web tab. Add the project
   directory to `sys.path`, configure the database and secret key, and import
   the lazy ASGI-to-WSGI adapter:

   ```python
   import os
   import sys

   project_dir = "/home/<username>/iskcon-book-inventory"
   if project_dir not in sys.path:
       sys.path.insert(0, project_dir)

   os.environ["DATABASE_PATH"] = (
       "/home/<username>/iskcon-book-inventory-data/inventory.db"
   )
   os.environ["SECRET_KEY"] = "<generate-a-long-random-secret>"

   from app.wsgi_adapter import application
   ```

   Replace `<username>` and set `SECRET_KEY` to a unique, randomly generated
   value. Keep both settings in the WSGI file and do not commit secrets. The
   adapter starts its event-loop thread on the first request so it is created
   inside the uWSGI worker, not before the worker starts.
5. Set the Web tab's source code and working directory to
   `/home/<username>/iskcon-book-inventory`, then reload the web app.

The app creates the schema and seeds demo data when it first starts. The seed
step only runs when there are no users. Change or remove the seeded demo
accounts immediately after first login. Never delete the separate data
directory when updating the checkout.

## Existing local database

If you need the inventory currently in `data/inventory.db`, copy that database
to `~/iskcon-book-inventory-data/inventory.db` before the first deployment.
Then ensure the WSGI `DATABASE_PATH` points to that copied file. Make a backup
before replacing it or importing further changes.

## Backing up the database

The repository includes `scripts/backup_database.py`. Run it from the project
checkout in a Bash console to create a timestamped, integrity-checked backup:

```bash
cd ~/iskcon-book-inventory
~/.virtualenvs/iskcon-book-inventory/bin/python scripts/backup_database.py \
  --database ~/iskcon-book-inventory-data/inventory.db \
  --backup-dir ~/inventory-backups
```

The script uses SQLite's online backup API, checks database integrity and
foreign-key consistency, and reports book, user, and stock counts. On
PythonAnywhere it restricts the backup directory and backup file permissions
to the account owner. Configure the **Tasks** tab to run this command daily if
scheduled tasks are available on your plan. Periodically download a backup to
another location and verify that it can be restored; backups on the same
account do not protect against loss of account access.

## Notes

- The free tier is intended for small projects and has storage, CPU, outbound
  network, and availability limits. Check PythonAnywhere's current free-account
  limits before relying on it for operational inventory.
- Keep a separate, regular backup of `inventory.db`; persistence on the host is
  not a backup.
- On a fresh database, the app creates demo users with known example passwords.
  Replace those credentials before allowing real users to access the app.
