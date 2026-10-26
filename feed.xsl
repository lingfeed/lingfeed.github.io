<?xml version="1.0" encoding="UTF-8"?>
<!-- Makes feed.xml readable when someone opens it in a browser, instead of showing raw XML.
     Feed readers ignore this file. -->
<xsl:stylesheet version="1.0" xmlns:xsl="http://www.w3.org/1999/XSL/Transform" xmlns:dc="http://purl.org/dc/elements/1.1/">
<xsl:output method="html" encoding="UTF-8" indent="yes"/>
<xsl:template match="/rss/channel">
<html lang="en">
<head>
<meta charset="utf-8"/>
<meta name="viewport" content="width=device-width, initial-scale=1"/>
<title>LingFeed RSS feed</title>
<link rel="icon" href="favicon.ico" sizes="any"/>
<style>
  :root { --bg:#fafaf7; --fg:#1c1c1a; --muted:#6b6b66; --line:#e2e1da; --link:#1a4f8b; --box:#e6eef8; }
  @media (prefers-color-scheme: dark) { :root { --bg:#181816; --fg:#e8e7e1; --muted:#9a9990; --line:#33322e; --link:#7db3f0; --box:#1f2b3b; } }
  body { margin:0; background:var(--bg); color:var(--fg); font:15px/1.45 Georgia,"Times New Roman",serif; }
  .wrap { max-width:760px; margin:0 auto; padding:24px 16px 64px; }
  h1 { font-size:26px; margin:0 0 12px; }
  a { color:var(--link); text-decoration:none; } a:hover { text-decoration:underline; }
  .box { background:var(--box); border-radius:8px; padding:12px 14px; font:14px/1.5 system-ui,sans-serif; margin-bottom:20px; }
  .box code { word-break:break-all; }
  .item { padding:10px 0; border-top:1px solid var(--line); }
  .meta { color:var(--muted); font:13px system-ui,sans-serif; margin-top:2px; }
</style>
</head>
<body><div class="wrap">
  <h1>LingFeed RSS feed</h1>
  <div class="box">
    This is LingFeed's RSS feed: the papers and new versions posted in the last two weeks, newest first.
    To follow it, copy this page's address into a feed reader (for example NetNewsWire, Feedly or Inoreader):<br/>
    <code><xsl:value-of select="atom:link/@href" xmlns:atom="http://www.w3.org/2005/Atom"/></code><br/>
    Or browse LingFeed itself at <a href="{link}"><xsl:value-of select="link"/></a>.
  </div>
  <xsl:for-each select="item">
    <div class="item">
      <a href="{link}"><xsl:value-of select="title"/></a>
      <div class="meta">
        <xsl:for-each select="dc:creator"><xsl:value-of select="."/><xsl:if test="position() != last()">; </xsl:if></xsl:for-each>
        · <xsl:value-of select="category"/> · <xsl:value-of select="substring(pubDate, 6, 11)"/>
      </div>
    </div>
  </xsl:for-each>
</div></body>
</html>
</xsl:template>
</xsl:stylesheet>
