# Setlister

Plugin for [beets](https://github.com/sampsyo/beets) to generate playlists from the setlists of a given artist, using [setlist.fm](http://www.setlist.fm)


## Usage
1. Clone this project, or download [setlister.py](beetsplug/setlister.py), in to your configured pluginpath (e.g., `~/.beets`)
2. Add `setlister` to your configured beets plugins
3. Register for a setlist.fm API key [here](https://www.setlist.fm/settings/api)
4. Configure setlister to know where your playlists have to be placed
```yaml
setlister:
    playlist_dir: ~/Music/setlists
    api_key: <YOUR API KEY HERE>
    user: <YOUR SETLIST.FM USERNAME>  # optional, only for --attended
```
Now you can run `$ beets setlister artist` to download the artists' latest setlist to your configured playlist directory, or specify the concert date using the `--date` option.

### Attended concerts

`$ beet setlister --attended` generates one playlist per concert you have marked
as **attended** on setlist.fm, reading the username from the `user` config option
(or `--user <username>` to override it). Concerts whose tracks aren't in your
library are skipped, and every run regenerates the full set so newly imported
music is picked up. In this mode the `artist` argument, `--date` and `--play` are
ignored.

## Sample
```bash
$ beet setlister alt-j   
Setlist: alt-J at Zenith (17-02-2015) (19 tracks)
1 Hunger of the Pine: found
2 Fitzpleasure: found
3 Something Good: found
4 Left Hand Free: found
5 Dissolve Me: found
6 Matilda: found
7 Bloodflood: found
8 Bloodflood Pt. 2: found
9 Leon: not found
10 ❦ (Ripe & Ruin): found
11 Tessellate: found
12 Every Other Freckle: found
13 Taro: found
14 Warm Foothills: found
15 The Gospel of John Hurt: found
16 Lovely Day: found
17 Nara: found
18 Leaving Nara: found
19 Breezeblocks: found
Saved playlist at "/Users/tjs/Music/setlists/alt-J at Zenith (17-02-2015).m3u"

```
