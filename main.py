from typing import Dict, List, Any, Generator, Callable, Mapping, NoReturn
from spotapi.data.interfaces import LoggerProtocol
from spotapi import (
    Login,
    PrivatePlaylist,
    PublicPlaylist,
    Song,
    solver_clients,
    Logger,
    Config,
    NoopLogger,
    JSONSaver,
    SaverError
)
from colorama import Fore, Style
from PIL import Image, ImageTk
from tkinter import ttk
from io import BytesIO
import tkinter as tk
import validators
import threading
import requests
import atexit
import json
import os

class Once:
    """Inspired by golang's sync.Once"""
    def __init__(self, func: Callable[[], None]):
        self._lock = threading.Lock()
        self._executed = False
        self.func = func

    def run(self, *args, **kwargs):
        # Args and Kwargs are not used
        with self._lock:
            if not self._executed:
                self.func()
                self._executed = True

class ParserGUI(tk.Tk):
    def __init__(
        self,
        callback: Callable[[List[str]], None],
        getter_gen: Generator[List[Dict[str, Any]], None, None],
        logger: LoggerProtocol = NoopLogger(),
    ) -> None:
        # Used for atexit calls
        self.exit = Once(self.save_playlist)
        atexit.register(self.exit.run)
        
        super().__init__()
        
        # On close
        self.protocol("WM_DELETE_WINDOW", self.exit.run)

        self.title("Playlist Parser | https://github.com/Aran404/Spotify-Playlist-Parser")
        self.geometry("500x500")
        self.configure(bg="white")
        self.create_widgets()

        # Songs will be changed once yielded again
        # {"name": ..., "photo": ..., "artist": ..., "id": ...}
        self.songs: List[Dict[str, Any]] = next(getter_gen)

        if len(self.songs) == 0:
            raise ValueError("No songs found")

        # This callback is used once we are done with the GUI
        self.callback = callback

        self.getter_gen = getter_gen
        self.logger = logger
        self.for_removal: List[str] = []
        self.current_song_index = 0

    def create_widgets(self) -> None:
        self.photo_label = ttk.Label(self, background="white")
        self.photo_label.pack(pady=20)

        self.song_name_label = ttk.Label(
            self, font=("Helvetica", 16), background="white"
        )
        self.song_name_label.pack(pady=10)

        self.artist_name_label = ttk.Label(
            self, font=("Helvetica", 16), background="white"
        )
        self.artist_name_label.pack(pady=10)

        nav_frame = ttk.Frame(self)
        nav_frame.pack(pady=20)

        # buttons
        left_button = ttk.Button(nav_frame, text="<", command=self.remove_song)
        left_button.pack(side="left", padx=20)
        right_button = ttk.Button(nav_frame, text=">", command=self.add_song)
        right_button.pack(side="right", padx=20)

        # keyboard events
        self.bind("<Left>", self.remove_song)
        self.bind("<Right>", self.add_song)
        self.bind("<Control-s>", self.exit.run)
        self.bind("<Control-c>", lambda: os._exit(1))

    def display_current_song(self) -> None:
        if len(self.songs) == self.current_song_index:
            self.logger.attempt("Getting new song pagination")
            try:
                n = next(self.getter_gen)
            except StopIteration:
                self.exit.run()

            self.songs = n
            self.current_song_index = 0

        song = self.songs[self.current_song_index]
        self.song_name_label.config(text=song["name"])
        self.artist_name_label.config(text=song["artist"])

        if not validators.url(song["photo"]):
            raw = BytesIO(song["photo"])
        else:
            raw = BytesIO(requests.get(song["photo"]).content)

        image = Image.open(raw)
        image = image.resize((200, 200), Image.Resampling.LANCZOS)
        photo = ImageTk.PhotoImage(image)
        self.photo_label.config(image=photo)
        self.photo_label.image = photo

    def save_playlist(self, event=None) -> NoReturn:
        try:
            # Destroy before callback to strip user interaction
            self.destroy()
            self.callback(self.for_removal)
            self.logger.info("Saved playlist")
            os._exit(1)
        except: 
            pass

    def remove_song(self, event=None) -> None:
        self.logger.error(
            "Removing song",
            name=self.songs[self.current_song_index]["name"],
            artist=self.songs[self.current_song_index]["artist"],
        )
        self.for_removal.append(self.songs[self.current_song_index]["uid"])
        self.current_song_index = self.current_song_index + 1
        self.display_current_song()

    def add_song(self, event=None) -> None:
        self.logger.info(
            "Keeping song",
            name=self.songs[self.current_song_index]["name"],
            artist=self.songs[self.current_song_index]["artist"],
        )
        # We don't need to do anything here
        self.current_song_index = self.current_song_index + 1
        self.display_current_song()


class Parser:
    def __init__(
        self,
        playlist: str,
        cfg: Config,
        email: str,
        password: str,
        *,
        must: Callable[[Mapping[str, Any]], bool] = lambda _: True,
    ) -> None:
        saver = JSONSaver()
        
        self.rmust = must
        self.logger = cfg.logger
        
        try:
            self.ln = Login.from_saver(saver, cfg, email)
            self.logger.info("Found session in cache")
        except SaverError: 
            self.ln = Login(cfg, password, email=email)
            self.ln.login()
            self.ln.save(saver)
            self.logger.info("Saved session to cache")

        self.playlist = PrivatePlaylist(self.ln, playlist)
        self.pb_playlist = PublicPlaylist(playlist)

        # Temporary storage for removals
        self._removal: List[str] = []
        
        self.tk = ParserGUI(self.save, self.get_songs(), cfg.logger)
        self.tk.for_removal.extend(self._removal)
        self.tk.display_current_song()

    def must(self, x: Mapping[str, Any]) -> bool:
        data = x["itemV2"]["data"]
        rm = self.rmust(data)
        
        if not rm:
            artist = data["albumOfTrack"]["artists"]["items"][0]["profile"]["name"]
            self.logger.info("Auto Removing", name=data["name"], artist=artist)
            self._removal.append(x["uid"])

        return rm

    def save(self, songs: List[str]) -> None:
        s = Song(self.playlist)
        for song in songs:
            s.remove_song_from_playlist(uid=song)

    def get_songs(self) -> Generator[List[Dict[str, Any]], None, None]:
        for c in self.pb_playlist.paginate_playlist():
            # Messy but parses the data efficiently
            parsed = [
                {
                    "name": x["itemV2"]["data"]["name"],
                    "artist": x["itemV2"]["data"]["albumOfTrack"]["artists"]["items"][
                        0
                    ]["profile"]["name"],
                    "photo": x["itemV2"]["data"]["albumOfTrack"]["coverArt"]["sources"][
                        0
                    ]["url"],
                    "uid": x["uid"],
                }
                for x in c["items"]
                if (x["itemV2"]["data"] and self.must(x))
            ]
            yield parsed


if __name__ == "__main__":
    os.system("cls" if os.name == "nt" else "clear")
    
    # Must is a way of filtering out songs before they get shown to user. 
    # * Customizable
    def must(x: Mapping[str, Any]) -> bool:
        try:
            int(x["playcount"])
        except ValueError:
            return True

        return int(x["playcount"]) >= 10_000_000
    
    with open("config.json") as f:
        cfg = json.loads(f.read())
        
    playlist = input(
        f"{Style.BRIGHT}{Style.RESET_ALL}{Fore.LIGHTBLUE_EX}What playlist would you like to parse {Style.RESET_ALL}{Fore.LIGHTMAGENTA_EX}>> {Fore.RESET}{Style.RESET_ALL}"
    )

    os.system("cls" if os.name == "nt" else "clear")
    
    Parser(
        playlist,
        Config(
            solver_clients.Capsolver(cfg["capsolver_key"]),
            Logger(),
        ),
        cfg["email"],
        cfg["password"],
        must=lambda _: True, # must=must, # You can add a custom must
    ).tk.mainloop()