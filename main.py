# --- 1. HEALTH CHECK SERVER ---
import discord
import io
import os
import json
import threading
import re
import asyncio
from PIL import Image
from http.server import ThreadingHTTPServer, BaseHTTPRequestHandler

class HealthCheckHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.end_headers()
        self.wfile.write(b"Bot is running!")

def run_health_check():
    try:
        server = ThreadingHTTPServer(('0.0.0.0', 7860), HealthCheckHandler)
        server.serve_forever()
    except Exception as e:
        print(f"Health check server error: {e}")

threading.Thread(target=run_health_check, daemon=True).start()

# --- 2. CONFIGURATION & PERSISTENCE ---
TOKEN = os.getenv('DISCORD_TOKEN')
STORAGE_DIR = "/data/" if os.path.exists("/data/") else "/tmp/"
PREFS_FILE = os.path.join(STORAGE_DIR, "user_prefs.json")

# List of channels to track numbers in
TRACKED_CHANNELS = [1518305844591202494, 1518298488818106429]

def load_prefs():
    if os.path.exists(PREFS_FILE):
        try:
            with open(PREFS_FILE, "r") as f:
                return json.load(f)
        except:
            return {}
    return {}

def save_pref(user_id, device):
    try:
        prefs = load_prefs()
        prefs[str(user_id)] = device
        with open(PREFS_FILE, "w") as f:
            json.dump(prefs, f)
    except Exception as e:
        print(f"Failed to save user preference: {e}")

# --- 3. BOT INITIALIZATION ---
intents = discord.Intents.default()
intents.message_content = True
client = discord.Client(intents=intents)

# --- 4. IMAGE PROCESSING HELPERS ---

def build_collage(imgs_bytes, offsets):
    """
    Crops images and stacks them vertically into a combined output.
    If a single image has width > height, it avoids 1:1 cropping and preserves aspect ratio.
    """
    raw_imgs = [Image.open(io.BytesIO(data)) for data in imgs_bytes]
    if not raw_imgs:
        return None

    # If single image is landscape (width > height), do not crop to 1:1
    if len(raw_imgs) == 1 and raw_imgs[0].width > raw_imgs[0].height:
        out_binary = io.BytesIO()
        raw_imgs[0].save(out_binary, 'PNG')
        out_binary.seek(0)
        return out_binary

    # Determine canvas width (use the largest width among uploaded images)
    canvas_width = max(img.width for img in raw_imgs)
    canvas_height = canvas_width  # Enforce 1:1 ratio for the whole collage
    
    num_imgs = len(raw_imgs)
    target_slice_height = canvas_height // num_imgs

    cropped_slices = []
    for i, img in enumerate(raw_imgs):
        w, h = img.size
        offset = offsets[i]

        # Crop region height relative to image width
        crop_h = int(w / num_imgs)

        if h > crop_h:
            max_off = max(0, h - crop_h)
            valid_offset = max(0, min(offset, max_off))
            box = (0, valid_offset, w, valid_offset + crop_h)
            cropped = img.crop(box)
        else:
            cropped = img

        # Resize slice to fit the output canvas exact slice width/height
        resized_slice = cropped.resize((canvas_width, target_slice_height), Image.Resampling.LANCZOS)
        cropped_slices.append(resized_slice)

    # Combine all slices onto a 1:1 canvas
    canvas = Image.new('RGB', (canvas_width, canvas_height), (0, 0, 0))
    current_y = 0
    for slice_img in cropped_slices:
        canvas.paste(slice_img, (0, current_y))
        current_y += slice_img.height

    out_binary = io.BytesIO()
    canvas.save(out_binary, 'PNG')
    out_binary.seek(0)
    return out_binary

# --- 5. UI VIEWS ---

class ResolveNoticeView(discord.ui.View):
    """View attached to party number gap warnings to mark them resolved."""
    def __init__(self):
        super().__init__(timeout=None)

    @discord.ui.button(label="Mark as Resolved", style=discord.ButtonStyle.success, emoji="✅")
    async def resolve_callback(self, interaction: discord.Interaction, button: discord.ui.Button):
        original_text = interaction.message.content
        updated_text = f"~~{original_text}~~\n\n✅ **Resolved** by {interaction.user.mention}"
        await interaction.response.edit_message(content=updated_text, view=None)

class EditPostModal(discord.ui.Modal, title="Edit Post"):
    new_text = discord.ui.TextInput(
        label="Message Text",
        style=discord.TextStyle.paragraph,
        placeholder="Enter updated message content...",
        required=False,
        max_length=2000
    )

    def __init__(self, target_message, author_mention):
        super().__init__()
        self.target_message = target_message
        self.author_mention = author_mention

        existing_text = target_message.content.replace(f"{author_mention} ", "").replace(author_mention, "")
        self.new_text.default = existing_text

    async def on_submit(self, interaction: discord.Interaction):
        updated_content = f"{self.author_mention} {self.new_text.value}".strip()
        try:
            await self.target_message.edit(content=updated_content)
            await interaction.response.send_message("✅ Post updated successfully!", ephemeral=True)
        except Exception as e:
            await interaction.response.send_message(f"❌ Failed to edit post: {e}", ephemeral=True)

class ConfirmDeleteView(discord.ui.View):
    def __init__(self, original_message):
        super().__init__(timeout=60)
        self.original_message = original_message

    @discord.ui.button(label="Yes, Delete Post", style=discord.ButtonStyle.danger)
    async def confirm_callback(self, interaction: discord.Interaction, button: discord.ui.Button):
        try:
            await self.original_message.delete()
            await interaction.response.edit_message(content="✅ Post deleted.", view=None)
        except:
            await interaction.response.send_message("I couldn't delete that message.", ephemeral=True)

class MultiCropAdjustView(discord.ui.View):
    """Single ephemeral message view providing controls for all photos at once."""
    def __init__(self, target_message, imgs_bytes, offsets, owner_id):
        super().__init__(timeout=180)
        self.target_message = target_message
        self.imgs_bytes = imgs_bytes
        self.offsets = offsets
        self.owner_id = owner_id

        num_photos = len(imgs_bytes)

        if num_photos == 1:
            btn_up1 = discord.ui.Button(label="⬆️ Move Up", style=discord.ButtonStyle.secondary, row=0)
            btn_down1 = discord.ui.Button(label="⬇️ Move Down", style=discord.ButtonStyle.secondary, row=0)
            btn_up1.callback = lambda i: self.shift_photo(i, 0, -50)
            btn_down1.callback = lambda i: self.shift_photo(i, 0, 50)
            self.add_item(btn_up1)
            self.add_item(btn_down1)
        else:
            btn_up1 = discord.ui.Button(label="[1] ⬆️ Move Up", style=discord.ButtonStyle.primary, row=0)
            btn_down1 = discord.ui.Button(label="[1] ⬇️ Move Down", style=discord.ButtonStyle.primary, row=0)
            btn_up1.callback = lambda i: self.shift_photo(i, 0, -50)
            btn_down1.callback = lambda i: self.shift_photo(i, 0, 50)
            self.add_item(btn_up1)
            self.add_item(btn_down1)

            btn_up2 = discord.ui.Button(label="[2] ⬆️ Move Up", style=discord.ButtonStyle.secondary, row=1)
            btn_down2 = discord.ui.Button(label="[2] ⬇️ Move Down", style=discord.ButtonStyle.secondary, row=1)
            btn_up2.callback = lambda i: self.shift_photo(i, 1, -50)
            btn_down2.callback = lambda i: self.shift_photo(i, 1, 50)
            self.add_item(btn_up2)
            self.add_item(btn_down2)

        btn_done = discord.ui.Button(label="✅ Done", style=discord.ButtonStyle.success, row=2)
        btn_done.callback = self.done_callback
        self.add_item(btn_done)

    def can_manage(self, interaction: discord.Interaction) -> bool:
        is_owner = interaction.user.id == self.owner_id
        is_admin = interaction.user.guild_permissions.administrator if interaction.guild else False
        return is_owner or is_admin

    async def shift_photo(self, interaction: discord.Interaction, photo_idx: int, delta: int):
        if not self.can_manage(interaction):
            return await interaction.response.send_message("Permission denied.", ephemeral=True)

        self.offsets[photo_idx] += delta
        out_binary = build_collage(self.imgs_bytes, self.offsets)
        file = discord.File(fp=out_binary, filename="cropped.png")

        await interaction.response.defer()
        await self.target_message.edit(attachments=[file])

    async def done_callback(self, interaction: discord.Interaction):
        if not self.can_manage(interaction):
            return await interaction.response.send_message("Permission denied.", ephemeral=True)
        await interaction.response.send_message("✅ Crop positions saved!", ephemeral=True)
        self.stop()

class PostActionView(discord.ui.View):
    def __init__(self, owner_id, imgs_bytes=None, offsets=None):
        super().__init__(timeout=None)
        self.owner_id = owner_id
        self.imgs_bytes = imgs_bytes or []
        self.offsets = offsets or []

    def can_manage(self, interaction: discord.Interaction) -> bool:
        is_owner = interaction.user.id == self.owner_id
        is_admin = interaction.user.guild_permissions.administrator if interaction.guild else False
        return is_owner or is_admin

    @discord.ui.button(label="Adjust Crop", style=discord.ButtonStyle.secondary)
    async def adjust_crop_request(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not self.can_manage(interaction):
            return await interaction.response.send_message("Only the author or Server Administrators can adjust this crop!", ephemeral=True)

        if not self.imgs_bytes:
            return await interaction.response.send_message("Original image data unavailable.", ephemeral=True)

        view = MultiCropAdjustView(
            target_message=interaction.message,
            imgs_bytes=self.imgs_bytes,
            offsets=self.offsets,
            owner_id=self.owner_id
        )

        header_text = "📷 **Crop Adjustments:**" if len(self.imgs_bytes) == 1 else "📷 **Photo 1 & Photo 2 Crop Controls:**"
        await interaction.response.send_message(header_text, view=view, ephemeral=True)

    @discord.ui.button(label="Edit Post", style=discord.ButtonStyle.primary)
    async def edit_request(self, interaction: discord.Interaction, button: discord.ui.Button):
        if self.can_manage(interaction):
            author_mention = f"<@{self.owner_id}>"
            modal = EditPostModal(target_message=interaction.message, author_mention=author_mention)
            await interaction.response.send_modal(modal)
        else:
            await interaction.response.send_message("Only the author or Server Administrators can edit this post!", ephemeral=True)

    @discord.ui.button(label="Delete Post", style=discord.ButtonStyle.danger)
    async def delete_request(self, interaction: discord.Interaction, button: discord.ui.Button):
        if self.can_manage(interaction):
            view = ConfirmDeleteView(original_message=interaction.message)
            await interaction.response.send_message(content="⚠️ **Delete this post?**", view=view, ephemeral=True)
        else:
            await interaction.response.send_message("Only the author or Server Administrators can delete this post!", ephemeral=True)

class DeviceSelectView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=60)

    async def process_and_save(self, interaction, device):
        save_pref(interaction.user.id, device)
        device_name = "iOS" if device == "ios" else "Android"
        await interaction.response.send_message(
            content=f"✅ **Preference Saved!** I will now use **{device_name}** crops for your photos.",
            ephemeral=True
        )
        try:
            await interaction.message.delete()
        except:
            pass

    @discord.ui.button(label="Android", style=discord.ButtonStyle.primary)
    async def android_button(self, interaction, button):
        await self.process_and_save(interaction, "android")

    @discord.ui.button(label="iOS (iPhone)", style=discord.ButtonStyle.secondary)
    async def ios_button(self, interaction, button):
        await self.process_and_save(interaction, "ios")

# --- 6. CORE CROP & COLLAGE LOGIC ---

async def perform_crop(message, attachments, clean_text, default_offset):
    imgs_bytes = []
    offsets = []

    for attachment in attachments:
        try:
            b = await attachment.read()
            imgs_bytes.append(b)
            offsets.append(default_offset)
        except Exception as e:
            print(f"Error reading attachment: {e}")

    if not imgs_bytes:
        return False

    try:
        out_binary = build_collage(imgs_bytes, offsets)
        if not out_binary:
            return False

        file = discord.File(fp=out_binary, filename="cropped.png")
        view = PostActionView(owner_id=message.author.id, imgs_bytes=imgs_bytes, offsets=offsets)
        content = f"{message.author.mention} {clean_text}".strip()

        await message.channel.send(content=content, file=file, view=view, silent=True)

        try:
            await message.delete()
        except:
            pass
        return True
    except Exception as e:
        print(f"Error during collage creation/sending: {e}")
        return False

# --- 7. EVENTS ---

@client.event
async def on_ready():
    print(f'Logged in as {client.user.name}. Storage path: {STORAGE_DIR}')

@client.event
async def on_message(message):
    # HARD IGNORE: Instantly ignore any message sent by Nymeris
    if message.author.id == 1247291758857949224:
        return

    if message.author.bot and message.author != client.user:
        return

    missed_message = None
    
    # --- SEQUENCE CHECK LOGIC ---
    if message.channel.id in TRACKED_CHANNELS:
        match = re.search(r'#(\d+)', message.content)
        if match:
            current_num = int(match.group(1))
            actual_user_id = message.author.id
            actual_user_mention = message.author.mention
            
            if message.author == client.user and message.mentions:
                actual_user_id = message.mentions[0].id
                actual_user_mention = message.mentions[0].mention
            
            last_num = 0
            last_user_id = None
            resolved_numbers = set()
            
            async for past_msg in message.channel.history(limit=30, before=message):
                # Parse resolved notices and include missing numbers AND their adjacent boundary numbers
                if past_msg.author == client.user and "✅ **Resolved**" in past_msg.content:
                    range_match = re.search(r'#(\d+) through (\d+)', past_msg.content)
                    if range_match:
                        start_n, end_n = int(range_match.group(1)), int(range_match.group(2))
                        # Include missing range plus adjacent boundary numbers (start_n - 1) and (end_n + 1)
                        resolved_numbers.update(range(max(1, start_n - 1), end_n + 2))
                    else:
                        list_match = [int(x) for x in re.findall(r'#(\d+)', past_msg.content)]
                        if list_match:
                            for n in list_match:
                                resolved_numbers.update([n - 1, n, n + 1])

                if past_msg.author.id in (1463361569424543898, 1247291758857949224):
                    continue  
                    
                past_match = re.search(r'#(\d+)', past_msg.content)
                if past_match and last_num == 0:
                    last_num = int(past_match.group(1))
                    last_user_id = past_msg.author.id
                    if past_msg.author == client.user and past_msg.mentions:
                        last_user_id = past_msg.mentions[0].id
            
            if last_num != 0 and current_num > last_num + 1:
                # Exclude numbers marked resolved or adjacently resolved
                gap = [i for i in range(last_num + 1, current_num) if i not in resolved_numbers]
                
                if gap:
                    if len(gap) > 30:
                        missed_nums = f"{gap[0]} through {gap[-1]}"
                    else:
                        missed_nums = ", #".join([str(i) for i in gap])
                    
                    if last_user_id and str(last_user_id) != str(actual_user_id):
                        tags = f"<@{last_user_id}> {actual_user_mention}"
                    else:
                        tags = actual_user_mention
                        
                    missed_message = f"{tags} ⚠️ Party number #{missed_nums} is missing."

    if message.author == client.user:
        if missed_message:
            await message.channel.send(
                missed_message, 
                allowed_mentions=discord.AllowedMentions.all(), 
                view=ResolveNoticeView(), 
                silent=True
            )
        return

    # --- IMAGE & CROP LOGIC ---
    image_attachments = [a for a in message.attachments if a.filename.lower().endswith(('png', 'jpg', 'jpeg', 'webp'))]
    clean_text = message.content
    for mention in message.mentions:
        if mention == client.user:
            clean_text = clean_text.replace(mention.mention, "")
    clean_text = clean_text.strip()

    is_reply = message.reference is not None
    if client.user.mentioned_in(message) and not is_reply:
        if "reset" in clean_text.lower():
            prefs = load_prefs()
            user_id_str = str(message.author.id)
            if user_id_str in prefs:
                del prefs[user_id_str]
                with open(PREFS_FILE, "w") as f:
                    json.dump(prefs, f)
            await message.channel.send("✅ Preference cleared.", delete_after=5, silent=True)
            try: await message.delete()
            except: pass
            return

        if not image_attachments:
            view = DeviceSelectView()
            await message.channel.send(
                f"{message.author.mention}, would you like to use Android or iOS crop settings?",
                view=view,
                silent=True
            )
            try: await message.delete()
            except: pass
            return

    crop_performed = False
    if image_attachments:
        prefs = load_prefs()
        user_id_str = str(message.author.id)
        ios_users = [730138298621886544, 1454173039942963333, 1489206924045189140, 970522360220368906]
        default_device = "ios" if message.author.id in ios_users else "android"
        device = prefs.get(user_id_str, default_device)
        offset = 185 if device == "ios" else 110
        crop_performed = await perform_crop(message, image_attachments, clean_text, offset)

    if missed_message:
        if crop_performed:
            await asyncio.sleep(2) 
        await message.channel.send(
            missed_message, 
            allowed_mentions=discord.AllowedMentions.all(), 
            view=ResolveNoticeView(), 
            silent=True
        )

# --- 8. START ---
if TOKEN:
    client.run(TOKEN)
else:
    print("FATAL ERROR: DISCORD_TOKEN is missing.")
