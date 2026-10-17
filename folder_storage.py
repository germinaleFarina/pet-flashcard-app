"""
folder_storage.py - dove vengono salvati i file di backup.

Due implementazioni con la stessa interfaccia (read_text, write_text, describe):

- FolderStorage: una cartella normale del filesystem. E' quella usata sul
  desktop (e nei test).
- AndroidTreeStorage: la cartella scelta dall'utente tramite il selettore di
  cartelle di Android (Storage Access Framework). Non richiede permessi:
  l'utente sceglie la cartella (es. Documenti/DigiVol) e Android concede
  all'app l'accesso, che resta valido anche dopo il riavvio e gli
  aggiornamenti dell'app. La cartella e i file restano anche se l'app viene
  disinstallata.

ATTENZIONE: la parte Android usa pyjnius e NON e' stata provata su un
telefono vero (sul PC non si puo'). Ogni errore viene comunque catturato e
mostrato a schermo come messaggio, senza far chiudere l'app.

Le specifiche salvate nelle impostazioni hanno la forma:
    "dir:/percorso/della/cartella"      (desktop)
    "android:content://...tree/..."     (Android)
"""

import os
from pathlib import Path


class StorageError(Exception):
    pass


class FolderStorage:
    def __init__(self, path):
        self.path = Path(path)
        try:
            self.path.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            raise StorageError(f"cannot use folder {self.path}: {exc}") from exc

    def read_text(self, name):
        target = self.path / name
        if not target.exists():
            return None
        try:
            return target.read_text(encoding="utf-8-sig")
        except (OSError, UnicodeDecodeError) as exc:
            raise StorageError(f"cannot read {name}: {exc}") from exc

    def write_text(self, name, text):
        target = self.path / name
        tmp = self.path / (name + ".tmp")
        try:
            tmp.write_text(text, encoding="utf-8", newline="\n")
            os.replace(tmp, target)  # sostituzione atomica: niente file a meta'
        except OSError as exc:
            raise StorageError(f"cannot write {name}: {exc}") from exc

    def describe(self):
        return str(self.path)


class AndroidTreeStorage:
    """Cartella scelta dall'utente (URI 'tree' del Storage Access Framework)."""

    def __init__(self, tree_uri):
        try:
            from jnius import autoclass

            self._Uri = autoclass("android.net.Uri")
            self._DocumentsContract = autoclass("android.provider.DocumentsContract")
            self._Document = autoclass("android.provider.DocumentsContract$Document")
            activity = autoclass("org.kivy.android.PythonActivity").mActivity
            self._resolver = activity.getContentResolver()
            self._tree_uri_text = tree_uri
            self._tree = self._Uri.parse(tree_uri)
            self._tree_doc_id = self._DocumentsContract.getTreeDocumentId(self._tree)
            self._dir_uri = self._DocumentsContract.buildDocumentUriUsingTree(
                self._tree, self._tree_doc_id
            )
        except Exception as exc:  # noqa: BLE001 - qualunque errore JNI
            raise StorageError(f"cannot open the selected folder: {exc}") from exc

    def describe(self):
        # "primary:Documents/DigiVol" -> "Documents/DigiVol"
        try:
            doc_id = str(self._tree_doc_id)
            return doc_id.split(":", 1)[1] if ":" in doc_id and doc_id.split(":", 1)[1] else doc_id
        except Exception:  # noqa: BLE001
            return self._tree_uri_text

    def _find(self, name):
        children = self._DocumentsContract.buildChildDocumentsUriUsingTree(
            self._tree, self._tree_doc_id
        )
        cursor = self._resolver.query(
            children,
            [self._Document.COLUMN_DOCUMENT_ID, self._Document.COLUMN_DISPLAY_NAME],
            None, None, None,
        )
        if cursor is None:
            raise StorageError("the folder cannot be listed")
        try:
            while cursor.moveToNext():
                if cursor.getString(1) == name:
                    return self._DocumentsContract.buildDocumentUriUsingTree(
                        self._tree, cursor.getString(0)
                    )
        finally:
            cursor.close()
        return None

    def read_text(self, name):
        try:
            uri = self._find(name)
            if uri is None:
                return None
            descriptor = self._resolver.openFileDescriptor(uri, "r")
            with os.fdopen(descriptor.detachFd(), "rb") as handle:
                return handle.read().decode("utf-8-sig")
        except StorageError:
            raise
        except Exception as exc:  # noqa: BLE001
            raise StorageError(f"cannot read {name}: {exc}") from exc

    def write_text(self, name, text):
        try:
            uri = self._find(name)
            if uri is None:
                uri = self._DocumentsContract.createDocument(
                    self._resolver, self._dir_uri, "text/plain", name
                )
                if uri is None:
                    raise StorageError(f"cannot create {name} in this folder")
            descriptor = self._resolver.openFileDescriptor(uri, "wt")  # scrive e svuota
            with os.fdopen(descriptor.detachFd(), "wb") as handle:
                handle.write(text.encode("utf-8"))
        except StorageError:
            raise
        except Exception as exc:  # noqa: BLE001
            raise StorageError(f"cannot write {name}: {exc}") from exc


def pick_folder_android(on_result):
    """Apre il selettore di cartelle di Android. on_result(uri_text) viene
    chiamata con l'URI della cartella scelta, oppure None se l'utente annulla.
    ATTENZIONE: on_result viene chiamata dal thread di Android, non da quello
    di Kivy: chi la fornisce deve rientrare nel thread di Kivy (@mainthread)."""
    from android import activity as android_activity  # noqa: WPS433 - solo su Android
    from jnius import autoclass

    PythonActivity = autoclass("org.kivy.android.PythonActivity")
    Intent = autoclass("android.content.Intent")
    Activity = autoclass("android.app.Activity")
    request_code = 48211

    def handler(code, result_code, intent):
        if code != request_code:
            return
        android_activity.unbind(on_activity_result=handler)
        if result_code != Activity.RESULT_OK or intent is None or intent.getData() is None:
            on_result(None)
            return
        uri = intent.getData()
        grant = Intent.FLAG_GRANT_READ_URI_PERMISSION | Intent.FLAG_GRANT_WRITE_URI_PERMISSION
        # Permesso permanente: vale anche dopo la chiusura/aggiornamento dell'app.
        PythonActivity.mActivity.getContentResolver().takePersistableUriPermission(uri, grant)
        on_result(uri.toString())

    android_activity.bind(on_activity_result=handler)
    intent = Intent(Intent.ACTION_OPEN_DOCUMENT_TREE)
    intent.addFlags(
        Intent.FLAG_GRANT_READ_URI_PERMISSION
        | Intent.FLAG_GRANT_WRITE_URI_PERMISSION
        | Intent.FLAG_GRANT_PERSISTABLE_URI_PERMISSION
    )
    PythonActivity.mActivity.startActivityForResult(intent, request_code)


def open_storage(spec):
    """Ricostruisce lo storage a partire dalla specifica salvata nelle
    impostazioni ('dir:...' oppure 'android:...')."""
    if spec.startswith("dir:"):
        return FolderStorage(spec[4:])
    if spec.startswith("android:"):
        return AndroidTreeStorage(spec[8:])
    raise StorageError("unknown backup location")
