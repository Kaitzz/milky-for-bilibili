"""
邮件发送模块 —— 通过 SMTP 发送包含 Markdown 笔记附件的邮件
"""

from __future__ import annotations

import logging
import smtplib
from email.mime.application import MIMEApplication
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText

logger = logging.getLogger(__name__)


class EmailSender:
    """SMTP 邮件发送器。"""

    def __init__(
        self,
        smtp_host: str,
        smtp_port: int,
        smtp_user: str,
        smtp_password: str,
        sender_name: str = "Milky",
        use_ssl: bool = True,
    ):
        self.smtp_host = smtp_host
        self.smtp_port = smtp_port
        self.smtp_user = smtp_user
        self.smtp_password = smtp_password
        self.sender_name = sender_name
        self.use_ssl = use_ssl

    def send_notes(
        self,
        to_email: str,
        video_title: str,
        bvid: str,
        markdown_content: str,
    ) -> bool:
        """发送包含 .md 笔记附件的邮件。

        Args:
            to_email:         收件人邮箱
            video_title:      视频标题
            bvid:             视频 BV 号
            markdown_content: Markdown 笔记正文

        Returns:
            True 发送成功, False 失败
        """
        subject = f"[Milky] 为您整理《{video_title}》笔记 | {bvid}"

        # 邮件正文
        body = (
            f"Milky 为您整理了《{video_title}》| {bvid} 笔记，"
            f"请查看附件中的文档。\n\n"
            f"— Milky 视频总结助手"
        )

        # 构建邮件
        msg = MIMEMultipart()
        from_display = f"{self.sender_name} <{self.smtp_user}>"
        msg["From"] = from_display
        msg["To"] = to_email
        msg["Subject"] = subject

        # 正文
        msg.attach(MIMEText(body, "plain", "utf-8"))

        # .md 附件
        safe_title = "".join(
            c if c.isalnum() or c in (" ", "-", "_", ".", "（", "）") else "_"
            for c in video_title
        )[:60]
        filename = f"{safe_title}_{bvid}.md"
        attachment = MIMEApplication(
            markdown_content.encode("utf-8"),
            Name=filename,
        )
        attachment["Content-Disposition"] = f'attachment; filename="{filename}"'
        msg.attach(attachment)

        # 发送
        try:
            if self.use_ssl:
                server = smtplib.SMTP_SSL(self.smtp_host, self.smtp_port, timeout=30)
            else:
                server = smtplib.SMTP(self.smtp_host, self.smtp_port, timeout=30)
                server.starttls()
            server.login(self.smtp_user, self.smtp_password)
            server.sendmail(self.smtp_user, [to_email], msg.as_string())
            server.quit()
            logger.info("邮件发送成功: to=%s subject=%s", to_email, subject)
            return True
        except Exception:
            logger.exception("邮件发送失败: to=%s", to_email)
            return False
