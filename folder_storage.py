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
from urllib.parse import unquote


class StorageError(Exception):
    pass


def describe_spec(spec):
    """Nome leggibile della cartella a partire dalla specifica salvata,
    SENZA interpellare Android (sicuro da chiamare all'avvio).
    'android:content://...tree/primary%3ADocuments%2FDigiVol' -> 'Documents/DigiVol'"""
    if not spec:
        return ""
    if spec.startswith("dir:"):
        return spec[4:]
    if spec.startswith("android:"):
        tail = unquote(spec[8:].rsplit("/tree/", 1)[-1])
        if ":" in tail:
            volume, path = tail.split(":", 1)
            return path or volume
        return tail
    return spec


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

    def __init__(self, tree_uri, trace=None):
        # trace(testo): chiamata PRIMA di ogni passo che tocca Android. L'app
        # la usa per annotare su file il passo in corso: se Android chiude
        # l'app proprio li', al riavvio si sa quale chiamata era.
        self._trace = trace or (lambda step: None)
        try:
            self._trace("open folder: load Java classes")
            from jnius import autoclass

            self._Uri = autoclass("android.net.Uri")
            self._DocumentsContract = autoclass("android.provider.DocumentsContract")
            self._Document = autoclass("android.provider.DocumentsContract$Document")
            self._BufferedReader = autoclass("java.io.BufferedReader")
            self._InputStreamReader = autoclass("java.io.InputStreamReader")
            self._OutputStreamWriter = autoclass("java.io.OutputStreamWriter")
            self._JString = autoclass("java.lang.String")
            activity = autoclass("org.kivy.android.PythonActivity").mActivity
            self._resolver = activity.getContentResolver()
            self._tree_uri_text = tree_uri
            self._trace("open folder: parse the folder address")
            self._tree = self._Uri.parse(tree_uri)
            self._tree_doc_id = self._DocumentsContract.getTreeDocumentId(self._tree)
            self._dir_uri = self._DocumentsContract.buildDocumentUriUsingTree(
                self._tree, self._tree_doc_id
            )
        except Exception as exc:  # noqa: BLE001 - qualunque errore JNI
            raise StorageError(f"cannot open the selected folder: {exc}") from exc

    def describe(self):
        return describe_spec("android:" + self._tree_uri_text)

    def _find(self, name):
        self._trace(f"look for {name} in the folder")
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
            # Flussi Java normali (nessun passaggio di descrittori di file
            # tra Java e Python, che su Android recente puo' far abortire
            # il processo se la proprieta' del descrittore non e' perfetta).
            self._trace(f"read {name}: open stream")
            stream = self._resolver.openInputStream(uri)
            if stream is None:
                raise StorageError(f"cannot open {name}")
            try:
                self._trace(f"read {name}: read lines")
                reader = self._BufferedReader(self._InputStreamReader(stream, "UTF-8"))
                lines = []
                while True:
                    line = reader.readLine()
                    if line is None:
                        break
                    lines.append(line)
            finally:
                stream.close()
            return ("\n".join(lines) + "\n").lstrip("\ufeff") if lines else ""
        except StorageError:
            raise
        except Exception as exc:  # noqa: BLE001
            raise StorageError(f"cannot read {name}: {exc}") from exc

    def write_text(self, name, text):
        try:
            uri = self._find(name)
            if uri is None:
                self._trace(f"write {name}: create the file")
                uri = self._DocumentsContract.createDocument(
                    self._resolver, self._dir_uri, "text/plain", name
                )
                if uri is None:
                    raise StorageError(f"cannot create {name} in this folder")
            self._trace(f"write {name}: open stream")
            stream = self._resolver.openOutputStream(uri, "wt")  # scrive e svuota
            if stream is None:
                raise StorageError(f"cannot open {name} for writing")
            try:
                self._trace(f"write {name}: write text")
                writer = self._OutputStreamWriter(stream, "UTF-8")
                writer.write(self._JString(text))
                writer.flush()
                self._trace(f"write {name}: close stream")
            finally:
                stream.close()
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


def open_storage(spec, trace=None):
    """Ricostruisce lo storage a partire dalla specifica salvata nelle
    impostazioni ('dir:...' oppure 'android:...')."""
    if spec.startswith("dir:"):
        return FolderStorage(spec[4:])
    if spec.startswith("android:"):
        return AndroidTreeStorage(spec[8:], trace=trace)
    raise StorageError("unknown backup location")
