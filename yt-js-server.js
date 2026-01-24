const { Innertube } = require('youtubei.js');
const express = require('express');
const app = express();

let youtube;

(async () => {
    youtube = await Innertube.create();
    app.listen(3000, () => console.log('YouTubei.js info server running on port 3000'));
})();

app.get('/info/:id', async (req, res) => {
    try {
        const video = await youtube.getInfo(req.params.id);
        const basic_info = video.basic_info;
        
        const response = {
            id: basic_info.id,
            title: basic_info.title,
            description: basic_info.description,
            thumbnail: basic_info.thumbnail,
            duration: basic_info.duration,
            view_count: basic_info.view_count,
            uploader: basic_info.author,
            channel_id: basic_info.channel_id,
            # 関連動画の取得
            related_videos: video.watch_next_feed.results.map(v => ({
                id: v.id,
                title: v.title?.toString(),
                author: v.author?.name,
                thumbnail: v.thumbnails?.[0]?.url
            })).filter(v => v.id),
            # 字幕やフォーマット等の生データ
            formats: video.streaming_data?.formats
        };
        res.json(response);
    } catch (e) {
        res.status(500).json({ error: e.message });
    }
});
